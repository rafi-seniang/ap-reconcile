# Accounts Payable Reconciliation Pilot Summary

**Prepared for:** Finance & Accounts Payable Leadership, PT Nusantara Retail Group  
**Pilot Batch:** PT Prima Kertas Indonesia (September 2026, 16 PDF pages vs. 16 ERP entries)

---

## Executive Summary

Monthly supplier reconciliation currently forces the AP team to check every line item across digital PDFs, skewed paper scans, multi-page invoices, and ERP spreadsheets by hand. On the September 2026 PT Prima Kertas Indonesia batch, our automated reconciliation tool completed the full document- and line-level check in **under 45 seconds** at an operating cost of **less than USD 0.02 per batch**, accounting for **100% of the Rp 3,256,673 gap** between supplier invoices and ERP records (**Rp 0 unexplained variance**).

The review also caught **Rp 17,238,178 in immediate duplicate payment risk**, **Rp 1,470,750 in supplier billing overcharges**, and **Rp 315,841 in tax and invoice-addition errors** before cash left the bank.

---

## September 2026 Batch Findings at a Glance

| Reconciliation Outcome | Documents | Financial Impact (IDR) | Immediate AP Action |
| --- | ---: | ---: | --- |
| **Cleared for Payment** (exact match + Rp 1 tax rounding) | 7 | -1 | Proceed on normal payment schedule |
| **Duplicate Booking in ERP** (`0147`, booked twice) | 1 | -17,238,178 | Hold payment and reverse duplicate entry `E-10460` |
| **Supplier Billing Overcharges** (`0144` qty, `0145` price, `0151` unapproved delivery fee) | 3 | +1,470,750 | Verify receiving slips and PO terms before paying |
| **Invoice Missing from ERP** (`0146`) | 1 | +15,688,629 | Confirm goods receipt against `PO-88215` and book if valid |
| **Invoice Missing from Supplier PDF** (`0155`, Parked in ERP) | 1 | -3,493,714 | Request missing invoice copy before posting |
| **Compliance & Data Review Required** (`0148` wrong VAT base, `0149` invoice addition error, `0154` wrong PO number) | 3 | +315,841 | Request corrected tax/commercial invoices and fix ERP PO |
| **Total Explained Variance (PDF Net Rp 307,896,675 vs. ERP Net Rp 311,153,348)** | **16** | **-3,256,673** | **Unexplained Variance: Rp 0** |

---

## How the Tool Protects Your Finance Team

- **Zero silent guesswork:** The system never alters or auto-corrects a supplier's numbers. When an invoice contains a math error (`0149`) or calculates 12% VAT on the wrong tax base (`0148`), it flags the exact invoice and holds it for human review.
- **Every number is traceable:** Each exception links straight to the exact PDF page and line number alongside the exact ERP sheet and row (for example, `PDF Page 4 Line 2` vs. `AP_Lines Row 14`).
- **Ready for daily AP operations:** AP staff receive a 4-sheet Excel workbook ready for CFO sign-off plus a local Review Queue screen showing each flagged invoice side by side with its original PDF page image.
- **Works on new months and new supplier layouts:** Tested on an unseen October batch with a different supplier layout, a 3-page invoice, and a skewed scan with **100% accuracy and Rp 0 unexplained variance**.

---

## Honest Boundaries and Human Controls

- **Human approval stays in charge:** The tool prepares the exception list and recommends actions, while your AP team verifies warehouse receiving slips (GRNs), approves ERP reversals, and releases payments.
- **ERP export hygiene:** If a monthly ERP spreadsheet export has missing columns or corrupted dates/amounts, the tool stops immediately and lists every broken cell so the team can fix the export before reconciling.
- **Edge cases routed to staff:** Unusually blurry mobile photos, handwritten margin notes, or foreign-currency invoices are routed to manual review rather than guessed.

---

## Recommended Next Steps (2-Week Pilot)

1. **Run October 2026 live** with PT Prima Kertas Indonesia side by side with your AP lead to confirm time saved at month-end close.
2. **Onboard your top 5 suppliers by invoice volume** using 5 to 10 past reconciled invoices per supplier to verify 100% tie-out before expanding to all 40+ suppliers.
3. **Agree on two finance policies:** confirm whether wrong 11/12 VAT-base invoices should always require a revised Faktur Pajak before payment, and confirm whether warehouse receiving slips (GRNs) should be added in Phase 2 for full three-way matching.
