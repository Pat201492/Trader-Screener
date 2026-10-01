"""
Calculation linkbase: fetch, parse and store how a filing's lines add up (#261).

A tag alone cannot say where a line sits. Walmart files membership fees as
``OtherIncome`` *inside* total revenue; another filer books ``OtherIncome`` as
non-operating. The tag is identical; only the roll-up tells them apart. Every
XBRL filing ships a calculation linkbase (``*_cal.xml``) that states those
roll-ups as weighted parent/child arcs, e.g.

    Revenues = RevenueFromContractWithCustomerExcludingAssessedTax + OtherIncome

so the sum-of-children is checkable against the reported parent, and a line's
*role* (operating vs. not) is recoverable from where it hangs in the tree.

This module does three deterministic things, stdlib only (the model never
touches a linkbase):

  * ``find_cal_url`` — locate the ``*_cal.xml`` in a filing's ``index.json``,
    cache-first through ``EdgarClient``; ``None`` when the filing ships none.
  * ``parse_cal`` — read the linkbase XML into plain arc dicts, with each
    concept normalised to ``<prefix>:Name`` from its locator ``href`` fragment.
    Handles the 2003 ``summation-item`` arcrole and Calculation 1.1 alike.
  * ``check_rollups`` — for one period's ``{concept: value}``, confirm each
    parent equals the weighted sum of its children (negative weights subtract),
    within a relative tolerance. A parent missing any child is skipped, not
    guessed at.

The parsed arcs land beside the facts, in ``facts_store.calc_arcs`` — see
``FactsStore.put_arcs`` / ``arcs_for``.
"""
import xml.etree.ElementTree as ET

try:  # package import
    from .edgar_client import cik_bare
except ImportError:  # flat import (script / pytest via conftest)
    from edgar_client import cik_bare


# ── locating the *_cal.xml in a filing ─────────────────────────────────────────

def find_cal_url(client, cik, accession):
    """Return the Archives URL of the filing's ``*_cal.xml``, or ``None``.

    Reads the accession's ``index.json`` manifest through ``client`` (which is
    cache-first — ``EdgarClient.filing_index`` serves a cached manifest without
    touching the network), scans the directory items for the one whose name ends
    ``_cal.xml``, and builds its Archives URL. A filing with no calculation
    linkbase (some small forms ship none) returns ``None`` rather than raising.
    """
    index = client.filing_index(cik, accession)
    items = ((index.get("directory", {}) or {}).get("item", []) or [])
    for it in items:
        name = (it.get("name") or "")
        if name.lower().endswith("_cal.xml"):
            acc = str(accession).replace("-", "")
            return (f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik_bare(cik)}/{acc}/{name}")
    return None


# ── parsing the linkbase XML ───────────────────────────────────────────────────

def _local(tag):
    """Local name of a (possibly namespaced) ElementTree tag: ``{ns}x`` -> ``x``."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _attr(elem, name):
    """An attribute by LOCAL name, namespace-agnostic. XBRL linkbases carry the
    load-bearing attributes (``role``, ``arcrole``, ``label``, ``from``, ``to``,
    ``href``) in the XLink namespace, but filers vary the prefix — match on the
    local part so ``xlink:from`` and a bare ``from`` both resolve."""
    for key, val in elem.attrib.items():
        if _local(key) == name:
            return val
    return None


def _concept_from_href(href):
    """Normalise a locator ``href`` fragment to ``<prefix>:Name``.

    An ``href`` is ``<schema>.xsd#us-gaap_Revenues``; the fragment after ``#`` is
    the element id, in which the FIRST underscore separates the taxonomy prefix
    from the concept name (``us-gaap_RevenueFromContract...`` -> ``us-gaap`` +
    ``RevenueFromContract...``). The concept name itself never carries an
    underscore, so splitting on the first one is exact."""
    if not href:
        return None
    frag = href.split("#", 1)[1] if "#" in href else href
    if "_" in frag:
        prefix, name = frag.split("_", 1)
        return f"{prefix}:{name}"
    return frag


def parse_cal(xml_text):
    """Parse a calculation linkbase into a list of arc dicts.

    Each arc is ``{role, parent, child, weight, order}``:

      * ``role`` — the ``calculationLink``'s ``xlink:role`` URI (the statement the
        roll-up belongs to);
      * ``parent`` / ``child`` — concepts normalised to ``<prefix>:Name``,
        resolved from the ``loc`` locators the arc's ``from``/``to`` labels point
        at (``parent`` is the summation, ``child`` an addend);
      * ``weight`` — a float; ``-1`` where a line is *subtracted* (a cost inside a
        subtotal);
      * ``order`` — a float where present, else ``None`` (presentation order).

    Both the 2003 ``.../arcrole/summation-item`` and Calculation 1.1
    (``summation-item`` under the 2023 arcrole) are accepted — matched on the
    arcrole's ``summation-item`` suffix, so the namespace year does not matter.
    A ``calculationArc`` whose ``from``/``to`` has no matching locator is skipped.
    """
    root = ET.fromstring(xml_text)
    arcs = []
    for link in root.iter():
        if _local(link.tag) != "calculationLink":
            continue
        role = _attr(link, "role")
        # label -> concept, from this link's locators
        label_concept = {}
        for child in link:
            if _local(child.tag) != "loc":
                continue
            label = _attr(child, "label")
            concept = _concept_from_href(_attr(child, "href"))
            if label is not None and concept is not None:
                label_concept[label] = concept
        for child in link:
            if _local(child.tag) != "calculationArc":
                continue
            arcrole = _attr(child, "arcrole") or ""
            if not arcrole.rstrip("/").endswith("summation-item"):
                continue
            parent = label_concept.get(_attr(child, "from"))
            kid = label_concept.get(_attr(child, "to"))
            if parent is None or kid is None:
                continue
            weight_raw = _attr(child, "weight")
            order_raw = _attr(child, "order")
            arcs.append({
                "role": role,
                "parent": parent,
                "child": kid,
                "weight": float(weight_raw) if weight_raw is not None else 1.0,
                "order": float(order_raw) if order_raw is not None else None,
            })
    return arcs


# ── checking a period's values against the roll-ups ────────────────────────────

def check_rollups(values, arcs, tolerance=0.005):
    """Confirm each parent equals the weighted sum of its children, for one period.

    ``values`` is ``{concept: number}`` for a single period; ``arcs`` is the list
    ``parse_cal`` returns. Arcs are grouped by ``(role, parent)`` — the same
    concept can roll up differently in two statements — and each group yields one
    result dict ``{parent, expected, reported, children, ok}``:

      * ``expected`` — ``Σ weight·value[child]`` (a ``-1`` weight subtracts);
      * ``reported`` — ``values[parent]``;
      * ``children`` — the child concepts summed, in arc order;
      * ``ok`` — ``|reported − expected| ≤ tolerance·|reported|``.

    A group is SKIPPED (not reported) when the parent or ANY child is absent from
    ``values``: a roll-up is only checkable when every term is present, and a
    missing addend would otherwise read as a false discrepancy.
    """
    groups = {}
    order = []
    for arc in arcs:
        key = (arc["role"], arc["parent"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(arc)

    results = []
    for key in order:
        role, parent = key
        group = groups[key]
        children = [a["child"] for a in group]
        if parent not in values or any(c not in values for c in children):
            continue
        expected = sum(a["weight"] * values[a["child"]] for a in group)
        reported = values[parent]
        ok = abs(reported - expected) <= tolerance * abs(reported)
        results.append({
            "parent": parent,
            "expected": expected,
            "reported": reported,
            "children": children,
            "ok": ok,
        })
    return results


if __name__ == "__main__":
    # Self-check against the hand-written fixture — no network, no clock.
    import json
    from pathlib import Path

    fx = Path(__file__).resolve().parent / "fixtures" / "cal"
    cal_text = (fx / "wmt-20250131_cal.xml").read_text(encoding="utf-8")
    arcs = parse_cal(cal_text)
    print(f"parsed {len(arcs)} arc(s)")
    for a in arcs:
        print(f"  {a['parent']} <- {a['child']}  (weight {a['weight']})")

    values = {
        "us-gaap:Revenues": 680985000000,
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": 674538000000,
        "us-gaap:OtherIncome": 6447000000,
    }
    for res in check_rollups(values, arcs):
        print(f"rollup {res['parent']}: expected {res['expected']} "
              f"reported {res['reported']} ok={res['ok']}")

    index = json.loads((fx / "index.json").read_text(encoding="utf-8"))

    class _FakeClient:
        def filing_index(self, cik, accession):
            return index

    print("cal url ->",
          find_cal_url(_FakeClient(), "104169", "0000104169-25-000012"))
    print("\ncalc_linkbase self-check: done")
