# Copyright (c) 2025, Frappe Technologies and contributors
# For license information, please see license.txt


import frappe
from frappe.model.document import Document

from ...m_pesa.utils import validate_phone_number
from ...m_pesa.api import initiate_stk_push


class MpesaExpressRequest(Document):
    def validate(self):
        if self.settings:
            self.payment_gateway = frappe.db.get_value(
                "Payment Gateway", {"gateway_controller": self.settings}, "name"
            )

        if not validate_phone_number(self.phone_number):
            frappe.throw(
                "Invalid phone number format. Please ensure it is in the correct format, e.g., 254712345678."
            )

    def on_submit(self):
        args = {
            "payment_gateway": self.payment_gateway,
            "phone_number": self.phone_number,
            "request_amount": self.amount,
            "doctype": self.doctype,
            "document_name": self.name,
            "reference_name": self.reference_name,
        }

        try:
            initiate_stk_push(**args)
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "STK Push on Submit Error")
            frappe.throw(f"Failed to initiate STK Push: {str(e)}")

    def on_update_after_submit(self):
        """Neutralize duplicate C2B when transaction_id is finally set after callback."""
        self.validate_duplicate_c2b_records()

    def validate_duplicate_c2b_records(self):
        """Ensure any duplicate C2B is neutralized in favour of this Express Request."""
        if not self.transaction_id:
            return

        c2b_name = frappe.db.exists(
            "Mpesa C2B Payment Register", {"transid": self.transaction_id}
        )

        if not c2b_name:
            return

        frappe.db.savepoint("before_c2b_neutralize")
        try:
            c2b_doc = frappe.get_doc("Mpesa C2B Payment Register", c2b_name)

            if c2b_doc.docstatus == 1:
                c2b_doc.cancel()
            else:
                c2b_doc.delete()

            frappe.log_error(
                message=f"Neutralised duplicate C2B {c2b_doc.name} in favour of Express Request {self.name}",
                title="Mpesa Express vs C2B Duplicate",
            )

        except Exception:
            frappe.db.rollback(save_point="before_c2b_neutralize")
            frappe.log_error(
                frappe.get_traceback(),
                f"Error neutralising duplicate C2B {c2b_name} for Express Request {self.name}",
            )
