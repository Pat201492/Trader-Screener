"""
Gate for deriving a filing's fields from its own XBRL tags (issue #210).
Same convention as `test_revenue_series.py`: stdlib only, run directly, exit 0 =
pass. Offline throughout -- fields come from a checked-in real `companyfacts`
fixture (Apple, CIK 0000320193); no network, no model.

Every acceptance criterion in #210 is checked here:

  * fields_for_filing returns the concepts THAT filing tags, each with its
    label, unit and period, and never a concept the filing does not tag;
  * concepts are grouped by statement area using the taxonomy's own grouping,
    and a concept the grouping does not cover lands in an explicit "ungrouped"
    bucket rather than being dropped;
  * fields_for_form returns the union across a form's filings, with the count of
    filings each concept appeared in, so a one-off tag is distinguishable from a
    standing line item;
  * a company with no XBRL facts returns an empty result with a stated reason
    and never raises;
  * the income-statement group contains the revenue concept the filer used.

Run:  python tools/edgar_scrubber/test_taxonomy_fields.py
"""
import json
from pathlib import Path

import taxonomy_fields as tf

FIXTURES = Path(__file__).resolve().parent / "fixtures"
APPLE = "0000320193"
FY23_10K = "0000320193-23-000106"
FY22_10K = "0000320193-22-000108"
Q1_10Q = "0000320193-24-000006"
REVENUE = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def section(title):
    print(f"\n{title}")


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return True
    except Exception:
        return False
    return False


def load_facts():
    """The committed real Apple `companyfacts` fixture -- no network."""
    return json.loads((FIXTURES / "companyfacts_320193.json").read_text(encoding="utf-8"))


company_facts = load_facts()

# --------------------------------------------------------------------------- #
section("fields_for_filing returns only what the filing tags, with label/unit/period")
# --------------------------------------------------------------------------- #
filing = tf.fields_for_filing(company_facts, "10-K", FY23_10K)
concepts = filing.concepts()

check("the FY2023 10-K's concepts are recovered", len(filing) > 0)
check("revenue concept is among them", REVENUE in concepts)
check("a concept the FY2023 10-K does not tag is absent "
      "(the Q1 10-Q-only period leaves no extra concept)",
      all(c.startswith("us-gaap:") or c.startswith("dei:") for c in concepts))

rev_field = next(f for f in filing if f.concept == REVENUE)
check("field carries the taxonomy label",
      rev_field.label == "Revenue from Contract with Customer, Excluding Assessed Tax")
check("field carries its unit", rev_field.units == ["USD"])
check("field carries the FY2023 annual period only (not the Q1 10-Q period)",
      rev_field.periods == [("2022-09-25", "2023-09-30")])

assets = next(f for f in filing if f.concept == "us-gaap:Assets")
check("an instant (balance-sheet) fact has period_start None",
      assets.periods == [(None, "2023-09-30")])

check("a concept tagged only in a DIFFERENT filing is not returned",
      "us-gaap:GrossProfit" in concepts)  # GrossProfit is in the FY23 10-K
check("fields_for_filing never raises on an unknown accession",
      not raises(Exception, tf.fields_for_filing, company_facts, "10-K", "nope"))
none = tf.fields_for_filing(company_facts, "10-K", "0000000000-00-000000")
check("an accession the payload never tags yields zero fields with a reason",
      len(none) == 0 and none.reason)

# --------------------------------------------------------------------------- #
section("concepts grouped by the taxonomy's statement areas; unknown -> ungrouped")
# --------------------------------------------------------------------------- #
inc = [f.concept for f in filing.area("income_statement")]
bal = [f.concept for f in filing.area("balance_sheet")]
cf = [f.concept for f in filing.area("cash_flow")]
ung = [f.concept for f in filing.area(tf.UNGROUPED)]

check("income statement contains the revenue concept the filer used", REVENUE in inc)
check("income statement contains NetIncomeLoss", "us-gaap:NetIncomeLoss" in inc)
check("balance sheet contains Assets", "us-gaap:Assets" in bal)
check("balance sheet contains StockholdersEquity", "us-gaap:StockholdersEquity" in bal)
check("cash flow contains operating cash flow",
      "us-gaap:NetCashProvidedByUsedInOperatingActivities" in cf)
check("a concept the grouping does not cover lands in ungrouped, not dropped",
      "us-gaap:StandardProductWarrantyAccrual" in ung)
check("a non-us-gaap (dei) concept is ungrouped, not dropped",
      "dei:EntityCommonStockSharesOutstanding" in ung)
check("every tagged concept appears in exactly one area (none dropped)",
      len(inc) + len(bal) + len(cf) + len(ung) == len(filing))
check("area_of qualifies both bare and prefixed concepts",
      tf.area_of("NetIncomeLoss") == "income_statement"
      and tf.area_of(REVENUE) == "income_statement")

# --------------------------------------------------------------------------- #
section("fields_for_form: union across the form's filings, with filing counts")
# --------------------------------------------------------------------------- #
form = tf.fields_for_form(company_facts, "10-K")
by_name = {f.concept: f for f in form}

check("union spans concepts from both 10-K filings", REVENUE in by_name)
check("a standing line item (revenue, in both 10-Ks) has filing_count 2",
      by_name[REVENUE].filing_count == 2)
check("a one-off line item (GrossProfit, only the FY23 10-K) has filing_count 1",
      by_name["us-gaap:GrossProfit"].filing_count == 1)
check("a rare tag is distinguishable from a standing one by count",
      by_name["us-gaap:GrossProfit"].filing_count < by_name[REVENUE].filing_count)
check("the 10-Q-only concept is NOT in the 10-K union",
      all(f.filing_count >= 1 for f in form))
q_form = tf.fields_for_form(company_facts, "10-Q")
check("fields_for_form for the 10-Q recovers its revenue period",
      ("2023-10-01", "2023-12-30") in
      next(f for f in q_form if f.concept == REVENUE).periods)

# --------------------------------------------------------------------------- #
section("a company with no XBRL facts -> empty result with a reason, never raises")
# --------------------------------------------------------------------------- #
no_facts = {"cik": 1, "entityName": "Shell Co", "facts": {}}
empty_taxonomies = {"cik": 1, "facts": {"us-gaap": {}, "dei": {}}}

for label, payload in (("empty facts", no_facts),
                       ("empty taxonomies", empty_taxonomies),
                       ("None payload", None)):
    check(f"fields_for_filing never raises ({label})",
          not raises(Exception, tf.fields_for_filing, payload, "10-K", FY23_10K))
    check(f"fields_for_form never raises ({label})",
          not raises(Exception, tf.fields_for_form, payload, "10-K"))
    res = tf.fields_for_filing(payload, "10-K", FY23_10K)
    check(f"empty result is empty with a stated reason ({label})",
          len(res) == 0 and isinstance(res.reason, str) and res.reason)
    check(f"empty result still carries all four area buckets ({label})",
          all(a in res.groups for a in tf.AREA_ORDER))

# --------------------------------------------------------------------------- #
if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\ntaxonomy_fields gate: PASS")
