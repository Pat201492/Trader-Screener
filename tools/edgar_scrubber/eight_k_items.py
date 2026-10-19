"""Form 8-K item codes and their official titles (#321).

EDGAR full-text search returns each 8-K hit's items (`_source.items`, e.g.
["2.02", "9.01"]), which say what happened without reading the filing. The
server filters and labels hits with this table, and the research assistant
validates a proposed search's `items` against it, so both share one list.

Section 6 has no items 6.07-6.09: Regulation AB jumps from 6.06 to 6.10.
"""

EIGHT_K_ITEMS = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety - Reporting of Shutdowns and Patterns of Violations",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation or an Obligation under an "
            "Off-Balance Sheet Arrangement of a Registrant",
    "2.04": "Triggering Events That Accelerate or Increase a Direct Financial "
            "Obligation or an Obligation under an Off-Balance Sheet Arrangement",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule "
            "or Standard; Transfer of Listing",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements or a Related "
            "Audit Report or Completed Interim Review",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Certain Officers; Election of Directors; "
            "Appointment of Certain Officers; Compensatory Arrangements of "
            "Certain Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.04": "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans",
    "5.05": "Amendment to Registrant's Code of Ethics, or Waiver of a Provision "
            "of the Code of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement or Other External Support",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    "6.06": "Static Pool",
    "6.10": "Alternative Filings of Asset-Backed Issuers",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


def normalize_items(items):
    """A list of known item codes from `items` (a list, or a comma string).
    Raises ValueError naming any unknown code -- never silently dropped."""
    if items is None:
        return []
    if isinstance(items, str):
        items = items.split(",")
    if not isinstance(items, (list, tuple)):
        raise ValueError("items must be a list of 8-K item codes")
    out, bad = [], []
    for it in items:
        code = str(it).strip()
        if not code:
            continue
        if code in EIGHT_K_ITEMS:
            if code not in out:
                out.append(code)
        else:
            bad.append(code)
    if bad:
        raise ValueError("unknown 8-K item code(s): %s" % ", ".join(bad))
    return out
