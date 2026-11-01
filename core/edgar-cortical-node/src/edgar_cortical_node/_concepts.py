"""Short item names -> XBRL tags in priority order.

For each company and period, the first tag in the list that has a value wins, so a company
that switched tags (e.g. to RevenueFromContractWithCustomer... in 2018) still gets one series.
kind:
    flow       amounts over a period (income statement, cash flow): quarters, Q4 and TTM derived
    instant    balances at a date (balance sheet)
    per_share  per-share amounts over a period: reported periods only (not additive)
unit: "currency" accepts any currency unit (USD for US filers, EUR/CAD/... for some foreign
filers); otherwise the exact XBRL unit.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Concept:
    kind: str
    unit: str
    tags: tuple[str, ...]  # "taxonomy:Tag"
    description: str = ""
    nonnegative: bool = False  # a negative derived quarter means mixed bases (see _pit.quarterize)


def _c(kind: str, unit: str, description: str, *tags: str, nonnegative: bool = False) -> Concept:
    return Concept(kind, unit, tags, description, nonnegative)


CONCEPTS: dict[str, Concept] = {
    # Income statement
    "revenue": _c("flow", "currency",
                  "Total revenue. Banks without a total use net revenue, then interest and dividend "
                  "income; REITs lease income; insurers earned premiums",
                  "us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                  "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax", "us-gaap:SalesRevenueNet",
                  "us-gaap:SalesRevenueGoodsNet", "us-gaap:SalesRevenueServicesNet",
                  "ifrs-full:Revenue", "ifrs-full:RevenueFromContractsWithCustomers",
                  "us-gaap:RevenuesNetOfInterestExpense", "us-gaap:OperatingLeaseLeaseIncome",
                  "us-gaap:LeaseIncome", "us-gaap:PremiumsEarnedNet",
                  "us-gaap:InterestAndDividendIncomeOperating",
                  nonnegative=True),
    "gross_profit": _c("flow", "currency", "Gross profit", "us-gaap:GrossProfit", "ifrs-full:GrossProfit"),
    "operating_income": _c("flow", "currency", "Operating income",
                           "us-gaap:OperatingIncomeLoss", "ifrs-full:ProfitLossFromOperatingActivities"),
    "net_income": _c("flow", "currency", "Net income attributable to the parent",
                     "us-gaap:NetIncomeLoss", "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
                     "us-gaap:ProfitLoss", "ifrs-full:ProfitLossAttributableToOwnersOfParent",
                     "ifrs-full:ProfitLoss"),
    "eps_basic": _c("per_share", "currency/shares", "Basic EPS",
                    "us-gaap:EarningsPerShareBasic", "ifrs-full:BasicEarningsLossPerShare"),
    "eps_diluted": _c("per_share", "currency/shares", "Diluted EPS",
                      "us-gaap:EarningsPerShareDiluted", "ifrs-full:DilutedEarningsLossPerShare"),
    # Cash flow
    "operating_cash_flow": _c("flow", "currency", "Net cash from operating activities",
                              "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                              "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
                              "ifrs-full:CashFlowsFromUsedInOperatingActivities"),
    "capex": _c("flow", "currency", "Purchases of property, plant and equipment",
                "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                "ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", nonnegative=True),
    "dividends_paid": _c("flow", "currency", "Dividends paid to common shareholders",
                         "us-gaap:PaymentsOfDividendsCommonStock", "us-gaap:PaymentsOfDividends",
                         "ifrs-full:DividendsPaidClassifiedAsFinancingActivities", nonnegative=True),
    # Balance sheet
    "assets": _c("instant", "currency", "Total assets", "us-gaap:Assets", "ifrs-full:Assets"),
    "liabilities": _c("instant", "currency", "Total liabilities", "us-gaap:Liabilities", "ifrs-full:Liabilities"),
    "equity": _c("instant", "currency", "Shareholders' equity (parent)",
                 "us-gaap:StockholdersEquity",
                 "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                 "ifrs-full:EquityAttributableToOwnersOfParent", "ifrs-full:Equity"),
    "cash": _c("instant", "currency", "Cash and cash equivalents",
               "us-gaap:CashAndCashEquivalentsAtCarryingValue",
               "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
               "ifrs-full:CashAndCashEquivalents"),
    "long_term_debt": _c("instant", "currency", "Long-term debt", "us-gaap:LongTermDebt",
                         "us-gaap:LongTermDebtNoncurrent", "ifrs-full:NoncurrentFinancialLiabilities"),
}

# Share counts, in precedence order (see EdgarStore.shares).
SHARE_TAGS = ("dei:EntityCommonStockSharesOutstanding", "us-gaap:CommonStockSharesOutstanding",
              "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic")
