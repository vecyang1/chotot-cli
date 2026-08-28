"""The contract re-measurement gate needs a gate of its own.

`doctor` is what tells a user their results may be wrong because upstream
changed. It had no tests, so a check that silently stopped being able to fail —
or one that reported PASS on the wrong condition — would never be noticed.

Each test here drives the real checks against a transport that returns a
*specific* upstream behaviour, and asserts both directions: the healthy case
passes and the drifted case does not.
"""
from __future__ import annotations

import json

import pytest

from chotot import doctor
from chotot.client import ChototClient
from chotot.formatter import Palette


class ScriptedTransport:
    """Answers each endpoint from a scripted table, so drift is expressible."""

    def __init__(self, ads, detail, behaviours=None) -> None:
        self._ads = ads
        self._detail = detail
        self.b = {
            "price_filter_works": True,
            "sp_ep_ignored": True,
            "total_capped": True,
            "absent_total": True,
            "offsets_differ": True,
            "limit_clamped": True,
            "sort_rejected": True,
            "seller_filter_works": True,
            "regions_populated": True,
            "detail_has_specs": True,
            "missing_is_404": True,
            "st_filters": True,
            "browse_is_mixed": True,
            "pages_overlap": True,
            "phone_masked": True,
            "merged_regions_populated": True,
        }
        self.b.update(behaviours or {})

    def get_json(self, path, params=None):
        from chotot.errors import NotFoundError, UsageError

        params = dict(params or {})
        if path.startswith("ad-listing/"):
            listing_id = path.split("/", 1)[1]
            if listing_id == "1":
                if self.b["missing_is_404"]:
                    raise NotFoundError("not found", remedy="check the id")
                return self._detail
            if not self.b["detail_has_specs"]:
                return {"ad": self._detail["ad"], "ad_params": {}}
            detail = json.loads(json.dumps(self._detail))
            detail["ad"]["phone"] = "034492****" if self.b["phone_masked"] else "0344921234"
            return detail

        if "sort" in params and "direction" in params:
            if self.b["sort_rejected"]:
                raise UsageError("HTTP 400", remedy="no server sort")
            return {"ads": self._ads[:5], "total": 10}

        if params.get("q") == "zzzznonexistentqueryxyz":
            return {"ads": []} if self.b["absent_total"] else {"ads": [], "total": 0}

        limit = int(params.get("limit", 20))
        if self.b["limit_clamped"]:
            limit = min(limit, 50)
        offset = int(params.get("o", 0))
        # Always produce a full page, whatever the offset: the deep-offset
        # checks ask for o=100 and o=150, and a fixture that runs out there
        # makes those checks fail for want of data rather than for a reason.
        base = offset if self.b["offsets_differ"] else 0
        rows = [dict(self._ads[(base + i) % len(self._ads)]) for i in range(limit)]
        for index, row in enumerate(rows):
            row["list_id"] = 10_000 + offset + index if self.b["offsets_differ"] else 10_000 + index
            if params.get("account_id"):
                row["account_id"] = params["account_id"] if self.b["seller_filter_works"] else 1

        if "price" in params:
            ceiling = int(str(params["price"]).split("-")[1] or 0)
            for row in rows:
                row["price"] = (ceiling - 1000 if self.b["price_filter_works"]
                                else ceiling + 1_000_000)
        if "sp" in params or "ep" in params:
            for row in rows:
                row["price"] = (99_000_000 if self.b["sp_ep_ignored"] else 1000)

        if params.get("region_v2") and not self.b["regions_populated"]:
            return {"ads": []}
        if params.get("region_v2") in (2010, 2011) and not self.b["merged_regions_populated"]:
            return {"ads": []}

        # Listing type: `st` filters when honoured; an unfiltered property
        # browse is mixed, which is what makes the check able to prove anything.
        if params.get("cg") == 1000:
            requested = params.get("st")
            for index, row in enumerate(rows):
                if requested and self.b["st_filters"]:
                    row["type"] = requested
                elif self.b["browse_is_mixed"]:
                    row["type"] = "s" if index % 2 else "u"
                else:
                    row["type"] = "s"

        # Adjacent offset windows repeat rows unless told otherwise. Only when
        # offsets are honoured at all -- otherwise this would perturb the
        # identical-windows case the pagination check looks for.
        if self.b["pages_overlap"] and self.b["offsets_differ"] and offset >= 100:
            for index, row in enumerate(rows[:5]):
                row["list_id"] = 10_000 + 100 + index

        payload = {"ads": rows}
        total = 10_000 if self.b["total_capped"] else 42
        if params.get("q") == "honda sh":
            total = 3090
        payload["total"] = total
        return payload


@pytest.fixture()
def healthy(sample_ads, detail_payload):
    return ChototClient(transport=ScriptedTransport(sample_ads, detail_payload))


def grade(client) -> dict:
    return {c.name: c.status for c in
            [check(client) for check in doctor.CHECKS]}


def test_a_healthy_gateway_passes_every_check(healthy):
    results = grade(healthy)
    failures = {n: s for n, s in results.items() if s == doctor.FAIL}
    assert not failures, failures
    assert len(results) == len(doctor.CHECKS) >= 10


@pytest.mark.parametrize("behaviour,check_name", [
    ({"price_filter_works": False}, "price range filter"),
    ({"total_capped": False}, "total cap"),
    ({"absent_total": False}, "no-match total is absent"),
    ({"offsets_differ": False}, "offset pagination"),
    ({"limit_clamped": False}, "limit clamp"),
    ({"sort_rejected": False}, "no server-side sort"),
    ({"seller_filter_works": False}, "account_id filter"),
    ({"regions_populated": False}, "province codes resolve"),
    ({"sp_ep_ignored": False}, "sp/ep still ignored"),
    ({"missing_is_404": False}, "missing listing -> 404"),
    ({"st_filters": False}, "listing type filter (st)"),
    ({"pages_overlap": False}, "adjacent pages overlap"),
    ({"phone_masked": False}, "listing phone stays masked"),
    ({"merged_regions_populated": False}, "merged provinces expand"),
])
def test_each_check_reacts_when_upstream_drifts(sample_ads, detail_payload, behaviour, check_name):
    """A check that cannot fail is not evidence, so each is shown failing."""
    client = ChototClient(transport=ScriptedTransport(sample_ads, detail_payload, behaviour))
    results = grade(client)
    assert results[check_name] in (doctor.FAIL, doctor.WARN), (
        f"{check_name} still reported {results[check_name]} with {behaviour}")


def test_doctor_exits_non_zero_when_a_check_fails(sample_ads, detail_payload, capsys):
    client = ChototClient(transport=ScriptedTransport(
        sample_ads, detail_payload, {"price_filter_works": False}))
    assert doctor.run_doctor(client, Palette(False)) == 1


def test_doctor_exits_zero_when_healthy(healthy, capsys):
    assert doctor.run_doctor(healthy, Palette(False)) == 0


def test_doctor_reports_how_many_subjects_it_graded(healthy, capsys):
    doctor.run_doctor(healthy, Palette(False))
    out = capsys.readouterr().out
    assert f"Graded {len(doctor.CHECKS)} subjects" in out


def test_json_mode_is_parseable_and_states_the_counts(healthy, capsys):
    doctor.run_doctor(healthy, Palette(False), as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["graded"] == len(doctor.CHECKS)
    assert len(payload["checks"]) == len(doctor.CHECKS)


def test_a_crashing_check_is_a_failing_check(sample_ads, detail_payload, monkeypatch):
    """A check that raises must not take the whole gate down silently."""
    def explode(client):
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "CHECKS", list(doctor.CHECKS) + [explode])
    client = ChototClient(transport=ScriptedTransport(sample_ads, detail_payload))
    assert doctor.run_doctor(client, Palette(False)) == 1
