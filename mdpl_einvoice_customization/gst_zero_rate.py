"""
Allow a "Taxable" Item Tax Template with GST Rate 0% (e.g. "GST 0% - MDPL")
to behave like any other taxable rate (5% / 12% / 18% / 28%).

India Compliance blocks this in two places:
  1. Item Tax Template validate -> "GST Rate cannot be zero for Taxable GST Treatment"
  2. Transaction validate_item_tax_template -> "No GST is being charged on Taxable Items"

Both functions are replaced here. `apply_patches` is wired to `before_request`
and `before_job` in hooks.py so the override is active in every web request and
background job (a plain module-level monkey patch is NOT reliable because app
packages are not imported when hooks are served from cache).

MAINTENANCE: after every india_compliance update, compare the functions below
with the originals in
  india_compliance/gst_india/overrides/item_tax_template.py  (validate)
  india_compliance/gst_india/overrides/transaction.py        (validate_item_tax_template)
and port any upstream changes.
"""

import frappe
from frappe import _, bold
from frappe.utils import flt

_PATCH_FLAG = "_mdpl_zero_rate_taxable_patched"


def apply_patches(*args, **kwargs):
	try:
		import india_compliance.gst_india.overrides.item_tax_template as ic_template
		import india_compliance.gst_india.overrides.transaction as ic_transaction
	except ImportError:
		return

	if getattr(ic_transaction, _PATCH_FLAG, False):
		return

	ic_template.validate = validate_item_tax_template_doc
	ic_transaction.validate_item_tax_template = validate_transaction_item_tax_template
	setattr(ic_transaction, _PATCH_FLAG, True)


# ---------------------------------------------------------------------------
# 1. Item Tax Template (doc_event: validate)
# ---------------------------------------------------------------------------


def validate_item_tax_template_doc(doc, method=None):
	"""Same as india_compliance, minus the 'GST Rate cannot be zero for Taxable' check."""
	from india_compliance.gst_india.overrides.item_tax_template import validate_tax_rates

	doc.gst_rate = flt(doc.gst_rate)
	if doc.gst_treatment != "Taxable":
		doc.gst_rate = 0

	validate_tax_rates(doc)


# ---------------------------------------------------------------------------
# 2. Transactions (Sales/Purchase Invoice, Order, Delivery Note, ...)
# ---------------------------------------------------------------------------


def validate_transaction_item_tax_template(doc):
	"""
	Copy of india_compliance's validate_item_tax_template. Only change: rows whose
	Item Tax Template is Taxable with GST Rate 0 are allowed to carry no GST.
	"""
	from india_compliance.gst_india.constants import TAXABLE_GST_TREATMENTS
	from india_compliance.gst_india.overrides.transaction import DOCTYPES_WITH_GST_DETAIL
	from india_compliance.gst_india.utils import is_import_transaction

	if doc.doctype not in DOCTYPES_WITH_GST_DETAIL:
		return

	if not doc.items or not doc.taxes:
		return

	is_import = is_import_transaction(doc)
	zero_rate_templates = get_zero_rate_taxable_templates(doc)
	non_taxable_items_with_tax = []
	taxable_items_with_no_tax = []

	for item in doc.items:
		if item.taxable_value == 0:
			continue

		if item.gst_treatment == "Zero-Rated" and not doc.get("is_export_with_gst"):
			continue

		is_gst_applied = bool(item.igst_rate + item.cgst_rate + item.sgst_rate)

		if is_gst_applied and item.gst_treatment not in TAXABLE_GST_TREATMENTS:
			non_taxable_items_with_tax.append(item.idx)

		if not is_gst_applied and item.gst_treatment in TAXABLE_GST_TREATMENTS:
			if is_import:
				continue
			# MDPL: Taxable @ 0% template -> no GST is expected
			if item.gst_treatment == "Taxable" and item.item_tax_template in zero_rate_templates:
				continue
			taxable_items_with_no_tax.append(item.idx)

	# Case: Zero Tax template with taxes or missing GST Accounts
	if non_taxable_items_with_tax:
		frappe.throw(
			_(
				"Cannot charge GST on Non-Taxable Items.<br>"
				"Are the taxes setup correctly in Item Tax Template? Please select"
				" the correct Item Tax Template for following row numbers:<br>{0}"
			).format(", ".join(bold(row_no) for row_no in non_taxable_items_with_tax)),
			title=_("Invalid Items"),
		)

	# Case: Taxable template with missing GST Accounts
	if taxable_items_with_no_tax:
		frappe.throw(
			_(
				"No GST is being charged on Taxable Items.<br>"
				"Are there missing GST accounts in Item Tax Template? Please"
				" verify the Item Tax Template for following row numbers:<br>{0}"
			).format(", ".join(bold(row_no) for row_no in taxable_items_with_no_tax)),
			title=_("Invalid Items"),
		)


def get_zero_rate_taxable_templates(doc):
	templates = {item.item_tax_template for item in doc.items if item.item_tax_template}
	if not templates:
		return set()

	return set(
		frappe.get_all(
			"Item Tax Template",
			filters={"name": ("in", list(templates)), "gst_treatment": "Taxable", "gst_rate": 0},
			pluck="name",
		)
	)
