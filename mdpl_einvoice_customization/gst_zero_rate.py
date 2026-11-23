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

Item Tax Template selection fallback (section 3 below):
ERPNext only picks an item's tax template when the Item Tax row's Tax Category
equals the transaction's Tax Category exactly. Item Tax rows here are kept with
a blank Tax Category, so for customers whose address carries a Tax Category
(e.g. "In-State") no template was picked and the 0% template was never applied
(the invoice-level 18% was charged instead, and the template dropdown was
empty). When nothing matches the transaction's category, the fallback retries
with a blank category.

MAINTENANCE: after every india_compliance / erpnext update, compare the
functions below with the originals in
  india_compliance/gst_india/overrides/item_tax_template.py  (validate)
  india_compliance/gst_india/overrides/transaction.py        (validate_item_tax_template)
  erpnext/stock/get_item_details.py                          (_get_item_tax_template)
and port any upstream changes. If erpnext starts importing
`_get_item_tax_template` in another module, add it to ERPNEXT_TAX_TEMPLATE_MODULES.
"""

import importlib

import frappe
from frappe import _, bold
from frappe.utils import cstr, flt

_PATCH_FLAG = "_mdpl_zero_rate_taxable_patched"
_FALLBACK_PATCH_FLAG = "_mdpl_item_tax_category_fallback_patched"

# Modules that hold their own reference to erpnext's `_get_item_tax_template`
# (via `from erpnext.stock.get_item_details import _get_item_tax_template`).
ERPNEXT_TAX_TEMPLATE_MODULES = (
	"erpnext.stock.get_item_details",
	"erpnext.controllers.accounts_controller",
	"erpnext.controllers.taxes_and_totals",
	"erpnext.controllers.queries",
)


def apply_patches(*args, **kwargs):
	_patch_india_compliance_zero_rate()
	_patch_item_tax_category_fallback()


def _patch_india_compliance_zero_rate():
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


# ---------------------------------------------------------------------------
# 3. Item Tax Template selection: fall back to blank Tax Category
# ---------------------------------------------------------------------------

_original_get_item_tax_template = None


def _patch_item_tax_category_fallback():
	global _original_get_item_tax_template

	try:
		import erpnext.stock.get_item_details as get_item_details
	except ImportError:
		return

	if getattr(get_item_details, _FALLBACK_PATCH_FLAG, False):
		return

	_original_get_item_tax_template = get_item_details._get_item_tax_template

	for module_name in ERPNEXT_TAX_TEMPLATE_MODULES:
		try:
			module = importlib.import_module(module_name)
		except ImportError:
			continue
		if getattr(module, "_get_item_tax_template", None) is _original_get_item_tax_template:
			module._get_item_tax_template = get_item_tax_template_with_category_fallback

	setattr(get_item_details, _FALLBACK_PATCH_FLAG, True)


def get_item_tax_template_with_category_fallback(ctx, taxes, out=None, for_validate=False):
	"""
	Wraps erpnext's `_get_item_tax_template`. If no Item Tax row matches the
	transaction's Tax Category, retry with a blank Tax Category so that the
	item's default (category-less) template is used. Behaviour is unchanged
	whenever a category-specific row matches.
	"""
	result = _original_get_item_tax_template(ctx, taxes, out, for_validate)
	if result:
		return result

	ctx = _ctx_to_dict(ctx)
	if not cstr(ctx.get("tax_category")):
		return result

	fallback_ctx = dict(ctx)
	fallback_ctx["tax_category"] = ""
	return _original_get_item_tax_template(fallback_ctx, taxes, out, for_validate)


def _ctx_to_dict(ctx):
	if isinstance(ctx, frappe.model.document.Document):
		return ctx.as_dict()
	if isinstance(ctx, dict):
		return ctx
	return frappe.parse_json(ctx) or {}
