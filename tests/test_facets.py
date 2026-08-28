"""Facet resolution.

The rule under test: a facet is offered only when a probe proved it filters.
Chợ Tốt declares ``elt_warranty`` and friends in the same posting-form schema as
the working facets and then ignores them at search time, so offering them would
hand back an unfiltered page labelled as filtered.
"""
from __future__ import annotations

import pytest

from chotot import facets
from chotot.errors import UsageError


def test_snapshot_is_present_and_probed():
    assert facets._all(), "facets.json is empty"
    assert facets.snapshot_date() != "unknown"


def test_phone_category_exposes_its_known_working_facets():
    available = facets.supported_params(5010)
    assert "mobile_brand" in available
    assert "mobile_capacity" in available


def test_facets_proven_to_be_ignored_are_not_offered():
    """These are declared by the schema and ignored by search."""
    for ignored in ("elt_warranty", "elt_condition", "mobile_type"):
        assert ignored not in facets.supported_params(5010)


def test_every_offered_facet_was_verified_working():
    """Range over ALL categories, not a sampled one, and report the count."""
    graded = 0
    for category in facets._all():
        for name, spec in facets.for_category(int(category)).items():
            graded += 1
            verdict = spec.get("verified") or {}
            assert verdict.get("works") is True, f"cg={category} {name} offered but unverified"
    print(f"graded {graded} offered facets across {len(facets._all())} categories")
    assert graded > 0


def test_human_label_resolves_to_the_gateway_code():
    assert facets.parse(5010, ["mobile_brand=apple"]) == {"mobile_brand": "1"}


def test_numeric_code_passes_through():
    assert facets.parse(5010, ["mobile_brand=1"]) == {"mobile_brand": "1"}


def test_unknown_option_is_refused_with_valid_values():
    with pytest.raises(UsageError) as excinfo:
        facets.parse(5010, ["mobile_brand=notarealbrand"])
    assert "Valid values" in (excinfo.value.remedy or "")


def test_ignored_facet_is_refused_with_the_measured_reason():
    with pytest.raises(UsageError) as excinfo:
        facets.parse(5010, ["elt_warranty=1"])
    assert "does not filter" in str(excinfo.value)


def test_unknown_facet_is_refused():
    with pytest.raises(UsageError):
        facets.parse(5010, ["not_a_facet=1"])


def test_malformed_pair_is_refused():
    with pytest.raises(UsageError):
        facets.parse(5010, ["mobile_brand"])


def test_facets_without_a_category_are_refused():
    """Resolving a facet needs to know which category's vocabulary applies."""
    with pytest.raises(UsageError):
        facets.parse(None, ["mobile_brand=apple"])


def test_empty_facet_list_is_a_no_op():
    assert facets.parse(5010, []) == {}
    assert facets.parse(None, []) == {}


def test_range_facet_requires_min_max():
    """A scalar year is accepted by the gateway and silently ignored."""
    car_facets = facets.for_category(2010)
    ranges = [n for n, s in car_facets.items() if s.get("is_range")]
    if not ranges:
        pytest.skip("no range facet in the current snapshot")
    name = ranges[0]
    with pytest.raises(UsageError) as excinfo:
        facets.parse(2010, [f"{name}=2019"])
    assert "MIN-MAX" in str(excinfo.value)
    assert facets.parse(2010, [f"{name}=2019-2021"]) == {name: "2019-2021"}


def test_every_advertised_facet_is_actually_forwardable():
    """A facet offered and then refused is an advertise-then-refuse pair.

    Five of these survived a green suite: `direction` is a verified property
    facet AND was the name of the sort modifier the contract blanket-refused.
    Ranges over every category and prints the count it graded.
    """
    from chotot import contract
    from chotot.errors import UnsupportedFilterError

    graded, broken = 0, []
    for category in facets._all():
        for name in facets.for_category(int(category)):
            graded += 1
            try:
                contract.assert_forwardable({name: "1"})
            except UnsupportedFilterError:
                broken.append(f"cg={category} {name}")
    print(f"graded {graded} (category, facet) pairs")
    assert graded > 100, f"only graded {graded} pairs; the selector narrowed"
    assert not broken, "facets advertised but refused by the contract guard: " + ", ".join(broken[:10])


def test_the_coherence_gate_can_fail():
    """A gate that has only ever seen a coherent pair is not evidence."""
    from chotot import contract
    from chotot.errors import UnsupportedFilterError
    import pytest as _pytest

    with _pytest.raises(UnsupportedFilterError):
        contract.assert_forwardable({"company_ad": "1"})


def test_an_ambiguous_folded_label_is_refused_not_guessed():
    """normalise() drops punctuation, so '> 256 GB' and '256 GB' collapse to one
    key. Returning whichever came first filtered on 'exactly 256GB' when the
    user asked for more.

    The message is asserted specifically: two later branches also refuse this
    input, so a loose assertion passes with the branch under test deleted.
    """
    with pytest.raises(UsageError) as excinfo:
        facets.parse(5010, ["mobile_capacity=256GB"])
    message = str(excinfo.value)
    assert "matches 2 options" in message, message
    # It must name BOTH readings, which is the whole point of refusing.
    assert "256 GB" in message and "> 256 GB" in message, message


def test_a_label_listed_under_two_codes_names_both_codes():
    """Upstream lists 'Midea' as both 4 and 9 (cg=14050). Nothing the user can
    type distinguishes them, so the refusal names the codes."""
    with pytest.raises(UsageError) as excinfo:
        facets.parse(14050, ["product_brand=Midea"])
    message = str(excinfo.value)
    assert "listed under 2 codes" in message, message
    assert "4, 9" in message, message
    assert "product_brand=4" in (excinfo.value.remedy or "")


def test_exact_labels_still_resolve_distinctly():
    """The negative side: refusing ambiguity must not break the exact forms."""
    assert facets.parse(5010, ["mobile_capacity=256 GB"]) == {"mobile_capacity": "7"}
    assert facets.parse(5010, ["mobile_capacity=> 256 GB"]) == {"mobile_capacity": "8"}


def test_no_two_options_of_one_facet_resolve_to_the_same_code_by_label():
    """Grade every facet, and report the count, so a snapshot that grows a new
    colliding pair fails rather than silently resolving to the wrong code."""
    from chotot.taxonomy import normalise

    graded, unresolvable = 0, []
    for category in facets._all():
        for name, spec in facets.for_category(int(category)).items():
            if spec.get("is_range"):
                continue  # a range facet needs MIN-MAX; a bare label is correctly refused
            options = spec.get("options") or {}
            for code, label in options.items():
                graded += 1
                # A raw code resolves to itself UNLESS the facet's labels are
                # themselves numbers that collide with codes (carseats). There
                # label wins by design, and describe() flags the facet.
                if not facets.codes_collide_with_labels(spec):
                    by_code = facets.parse(int(category), [f"{name}={code}"])
                    if by_code.get(name) != code:
                        unresolvable.append(
                            f"cg={category} {name} code {code} did not round-trip")

                # The label must resolve to that code, OR be refused as
                # ambiguous. Upstream lists a few labels under two codes
                # (cg=14050 product_brand "Midea" is both 4 and 9), and nothing
                # the user types can distinguish them -- so a refusal naming the
                # codes is correct and silently picking one is not.
                try:
                    resolved = facets.parse(int(category), [f"{name}={label}"])
                except UsageError as exc:
                    if "listed under" not in str(exc) and "matches" not in str(exc):
                        unresolvable.append(f"cg={category} {name}={label!r}: {exc}")
                    continue
                if resolved.get(name) != code:
                    unresolvable.append(
                        f"cg={category} {name}={label!r} resolved to "
                        f"{resolved.get(name)} not {code}")
    print(f"graded {graded} facet option labels")
    assert graded > 500, f"only graded {graded} labels"
    assert not unresolvable, unresolvable[:8]


def test_number_labelled_facets_resolve_by_label_and_are_flagged():
    """`carseats` is labelled "2","4","7" while its codes are 5,1,3.

    "--facet carseats=4" must mean four seats in every reading a user has, so
    the label wins; describe() flags the facet so the CLI can say codes are not
    reliable there.
    """
    spec = facets.for_category(2010)["carseats"]
    assert facets.codes_collide_with_labels(spec)
    four = [c for c, label in spec["options"].items() if label.strip() == "4"]
    assert facets.parse(2010, ["carseats=4"]) == {"carseats": four[0]}

    row = next(r for r in facets.describe(2010) if r["param"] == "carseats")
    assert row["use_labels_not_codes"] is True


def test_ordinary_facets_are_not_flagged():
    """The negative side: the flag must not fire on every facet."""
    row = next(r for r in facets.describe(5010) if r["param"] == "mobile_brand")
    assert row["use_labels_not_codes"] is False
