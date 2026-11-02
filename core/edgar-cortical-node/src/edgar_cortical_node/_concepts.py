"""Short item names -> XBRL tags in priority order.

For each company and period, the first tag in the list that has a value wins, so a company
that switched tags (e.g. to RevenueFromContractWithCustomer... in 2018) still gets one series.
kind:
    flow       amounts over a period (income statement, cash flow): quarters, Q4 and TTM derived
    instant    balances at a date (balance sheet)
    per_share  per-share amounts over a period: quarters derived as for flows; TTM is the annual
               figure at fiscal year ends, else four quarters summed (none across a split)
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
    second_quarter: bool = False  # 10-K cover figure measured at the end of the 2nd fiscal quarter


def _c(kind: str, unit: str, description: str, *tags: str, nonnegative: bool = False,
       second_quarter: bool = False) -> Concept:
    return Concept(kind, unit, tags, description, nonnegative, second_quarter)


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
    "cost_of_revenue": _c("flow", "currency", "Cost of revenue / cost of goods and services sold",
                          "us-gaap:CostOfRevenue", "us-gaap:CostOfGoodsAndServicesSold",
                          "ifrs-full:CostOfSales", nonnegative=True),
    "sga": _c("flow", "currency",
              "Selling, general and administrative expense (only where reported as one line; "
              "G&A alone is not used)",
              "us-gaap:SellingGeneralAndAdministrativeExpense",
              "ifrs-full:SellingGeneralAndAdministrativeExpense", nonnegative=True),
    "rnd": _c("flow", "currency", "Research and development expense",
              "us-gaap:ResearchAndDevelopmentExpense",
              "us-gaap:ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
              "ifrs-full:ResearchAndDevelopmentExpense", nonnegative=True),
    "depreciation_amortization": _c("flow", "currency",
                                    "Depreciation, depletion and amortization (depreciation alone is not used)",
                                    "us-gaap:DepreciationDepletionAndAmortization",
                                    "us-gaap:DepreciationAndAmortization",
                                    "us-gaap:DepreciationAmortizationAndAccretionNet",
                                    "ifrs-full:DepreciationAndAmortisationExpense",
                                    "ifrs-full:AdjustmentsForDepreciationAndAmortisationExpense", nonnegative=True),
    "interest_expense": _c("flow", "currency",
                           "Interest expense (for banks this includes interest on deposits; IFRS finance costs)",
                           "us-gaap:InterestExpense", "us-gaap:InterestExpenseNonoperating",
                           "us-gaap:InterestExpenseDebt", "ifrs-full:FinanceCosts", "ifrs-full:InterestExpense",
                           nonnegative=True),
    "pretax_income": _c("flow", "currency", "Income from continuing operations before income taxes",
                        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
                        "ifrs-full:ProfitLossBeforeTax"),
    "income_tax": _c("flow", "currency", "Income tax expense (benefit)",
                     "us-gaap:IncomeTaxExpenseBenefit", "ifrs-full:IncomeTaxExpenseContinuingOperations"),
    "eps_basic": _c("per_share", "currency/shares", "Basic EPS",
                    "us-gaap:EarningsPerShareBasic", "ifrs-full:BasicEarningsLossPerShare"),
    "eps_diluted": _c("per_share", "currency/shares", "Diluted EPS",
                      "us-gaap:EarningsPerShareDiluted", "ifrs-full:DilutedEarningsLossPerShare"),
    # Cash flow
    "operating_cash_flow": _c("flow", "currency", "Net cash from operating activities",
                              "us-gaap:NetCashProvidedByUsedInOperatingActivities",
                              "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
                              "ifrs-full:CashFlowsFromUsedInOperatingActivities"),
    "capex": _c("flow", "currency",
                "Purchases of property, plant and equipment (productive assets where PP&E is not "
                "tagged; oil and gas properties; REIT capital improvements and development). "
                "Real-estate acquisitions are not capex",
                "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                "us-gaap:PaymentsToAcquireProductiveAssets",
                "us-gaap:PaymentsToAcquireOilAndGasPropertyAndEquipment",
                "us-gaap:PaymentsToAcquireOilAndGasProperty",
                "us-gaap:PaymentsForCapitalImprovements", "us-gaap:PaymentsToDevelopRealEstateAssets",
                "ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", nonnegative=True),
    "dividends_paid": _c("flow", "currency", "Dividends paid to common shareholders",
                         "us-gaap:PaymentsOfDividendsCommonStock", "us-gaap:PaymentsOfDividends",
                         "us-gaap:PaymentsOfOrdinaryDividends",
                         "ifrs-full:DividendsPaidClassifiedAsFinancingActivities", nonnegative=True),
    "buybacks": _c("flow", "currency", "Repurchases of common stock",
                   "us-gaap:PaymentsForRepurchaseOfCommonStock",
                   "ifrs-full:PaymentsToAcquireOrRedeemEntitysShares", "ifrs-full:PurchaseOfTreasuryShares",
                   nonnegative=True),
    "stock_comp": _c("flow", "currency", "Share-based compensation (cash-flow add-back, else the expense)",
                     "us-gaap:ShareBasedCompensation", "us-gaap:AllocatedShareBasedCompensationExpense",
                     "ifrs-full:AdjustmentsForSharebasedPayments"),
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
    "current_assets": _c("instant", "currency", "Total current assets (not reported by banks and insurers)",
                         "us-gaap:AssetsCurrent", "ifrs-full:CurrentAssets"),
    "current_liabilities": _c("instant", "currency", "Total current liabilities (not reported by banks and insurers)",
                              "us-gaap:LiabilitiesCurrent", "ifrs-full:CurrentLiabilities"),
    "inventory": _c("instant", "currency", "Inventories, net", "us-gaap:InventoryNet", "ifrs-full:Inventories"),
    "receivables": _c("instant", "currency", "Accounts receivable, current, net",
                      "us-gaap:AccountsReceivableNetCurrent", "us-gaap:ReceivablesNetCurrent",
                      "ifrs-full:TradeAndOtherCurrentReceivables", "ifrs-full:CurrentTradeReceivables"),
    "goodwill": _c("instant", "currency", "Goodwill", "us-gaap:Goodwill", "ifrs-full:Goodwill"),
    "debt_current": _c("instant", "currency",
                       "Current maturities of long-term debt (short-term borrowings are a separate item)",
                       "us-gaap:LongTermDebtCurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent",
                       "ifrs-full:CurrentPortionOfLongtermBorrowings"),
    "short_term_borrowings": _c("instant", "currency", "Short-term borrowings (commercial paper where that is all)",
                                "us-gaap:ShortTermBorrowings", "us-gaap:CommercialPaper",
                                "ifrs-full:ShorttermBorrowings"),
    # 10-K cover page
    "public_float": _c("instant", "currency",
                       "Market value of common equity held by non-affiliates (10-K cover), as of the "
                       "last business day of the second fiscal quarter; `end` is that date (filings "
                       "that tag the fiscal year end instead are moved back, source='redated')",
                       "dei:EntityPublicFloat", nonnegative=True, second_quarter=True),
}

# Balance-sheet totals that date a filing's fiscal year end (see _pit.redate_second_quarter).
FISCAL_YEAR_END_TAGS = ("us-gaap:Assets", "us-gaap:StockholdersEquity", "ifrs-full:Assets")

# Share counts, in precedence order (see EdgarStore.shares).
SHARE_TAGS = ("dei:EntityCommonStockSharesOutstanding", "us-gaap:CommonStockSharesOutstanding",
              "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic")
