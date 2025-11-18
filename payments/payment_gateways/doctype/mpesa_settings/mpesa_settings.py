# Copyright (c) 2025, Frappe Technologies and contributors
# For license information, please see license.txt


import base64
from json import dumps, loads

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate

import frappe
from frappe import _, get_single
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import (
    fmt_money,
)
from frappe.utils.file_manager import get_file_path

from ...m_pesa.doctype_names import (
    PUBLIC_CERTIFICATES_DOCTYPE,
    MPESA_EXPRESS_REQUEST_DOCTYPE,
)
from ...m_pesa.utils import (
    erpnext_app_import_guard,
    create_payment_gateway_account,
    validate_phone_number,
)
from .mpesa_custom_fields import create_custom_pos_fields
from ...m_pesa.encoding_initiator_password import (
    generate_security_credential,
)

from ...m_pesa.api import get_account_balance


class MpesaSettings(Document):
    supported_currencies = ["KES"]

    def validate_transaction_currency(self, currency: str) -> None:
        """
        Validates that the transaction currency is supported for Mpesa.

        Allows the transaction if:
        - The currency is KES, OR
        - The company's default currency is KES
        """
        if currency in self.supported_currencies:
            return

        if self.company:
            default_currency = frappe.db.get_value(
                "Company", self.company, "default_currency"
            )
            if default_currency in self.supported_currencies:
                return

        frappe.throw(
            _(
                "Please select another payment method. Mpesa does not support transactions in currency '{0}'."
            ).format(currency)
        )

    def before_insert(self) -> None:
        """Before Insertion hook"""
        if self.api_type == "MPesa B2C (Business to Customer)":
            certificate_file = get_single(PUBLIC_CERTIFICATES_DOCTYPE)

            file_path = get_file_path(
                certificate_file.sandbox_certificate
                if self.sandbox
                else certificate_file.production_certificate
            )

            with open(file_path, "rb") as cert_file:
                public_key = load_pem_x509_certificate(
                    cert_file.read(), backend=default_backend()
                ).public_key()

            ciphertext = public_key.encrypt(
                self.online_passkey.encode("utf-8"),
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )

            self.security_credential = base64.b64encode(ciphertext).decode("utf-8")

    @frappe.whitelist()
    def get_payment_url(self, **kwargs) -> str:
        """Return the payment URL"""
        return "/all-products"

    def on_update(self) -> None:
        """On Update Hook"""
        from ....utils.utils import create_payment_gateway

        create_payment_gateway(
            "Mpesa-" + self.payment_gateway_name,
            settings="Mpesa Settings",
            controller=self.payment_gateway_name,
        )
        if "erpnext" in frappe.get_installed_apps():
            create_custom_pos_fields()

            create_payment_gateway_account(
                gateway="Mpesa-" + self.payment_gateway_name,
                payment_channel="Phone",
                company=self.company,
            )

            frappe.db.commit()  # nosemgrep
            create_mode_of_payment(
                "Mpesa-" + self.payment_gateway_name,
                payment_type="Phone",
                company=self.company,
            )

    def validate(self) -> None:
        if self.initiator_password and not self.security_credential:
            self.security_credential = generate_security_credential(
                (
                    self.get_password("initiator_password", "")
                    if self.initiator_password
                    else ""
                ),
                self.sandbox,
            )

    def request_for_payment(self, **kwargs) -> None:
        args = frappe._dict(kwargs)
        request_amounts = self.split_request_amount_according_to_transaction_limit(args)
        phone_number = args.get("phone_number")

        if not validate_phone_number(phone_number):
            sender = args.get("sender", "")
            if (
                isinstance(sender, str)
                and sender
                and (sender.startswith(("0", "254", "+", "7", "1")))
            ):
                phone_number = sender
        if not phone_number:
            frappe.throw(_("A valid phone number is required for Mpesa payment."))
        else:
            phone_number = sanitize_mobile_number(phone_number)

        for i, amount in enumerate(request_amounts):
            args.request_amount = amount
            if frappe.flags.in_test:
                from .test_mpesa_settings import get_payment_request_response_payload

                response = frappe._dict(get_payment_request_response_payload(amount))
            else:
                stk_request = frappe.new_doc(MPESA_EXPRESS_REQUEST_DOCTYPE)
                stk_request.update(
                    {
                        "amount": args.get("request_amount", 0.0),
                        "phone_number": phone_number,
                        "timestamp": frappe.utils.now(),
                        "settings": args.payment_gateway[6:],
                        "payment_gateway": args.get("payment_gateway"),
                        "reference_doctype": args.get("reference_doctype"),
                        "reference_name": args.get("reference_docname"),
                    }
                )
                stk_request.flags.ignore_permissions = True
                stk_request.insert(ignore_permissions=True)
                stk_request.submit()

    def split_request_amount_according_to_transaction_limit(
        self, args: frappe._dict
    ) -> list:
        request_amount = args.request_amount
        if request_amount > self.transaction_limit:
            request_amounts = []
            requests_to_be_made = frappe.utils.ceil(
                request_amount / self.transaction_limit
            )
            for i in range(requests_to_be_made):
                amount = self.transaction_limit
                if i == requests_to_be_made - 1:
                    amount = request_amount - (self.transaction_limit * i)
                request_amounts.append(amount)
        else:
            request_amounts = [request_amount]

        return request_amounts

    @frappe.whitelist()
    def get_account_balance_info(self) -> None:
        if frappe.flags.in_test:
            from .test_mpesa_settings import get_test_account_balance_response

            frappe._dict(get_test_account_balance_response())
        else:
            get_account_balance(self.name)

    def handle_api_response(
        self, global_id: str, request_dict: frappe._dict, response: frappe._dict
    ) -> None:
        """Response received from API calls returns a global identifier for each transaction, this code is returned during the callback."""
        if "requestId" in response:
            req_name = response["requestId"]
            error = response
        else:
            req_name = response[global_id]
            error = None

        if not frappe.db.exists("Integration Request", req_name):
            create_request_log(request_dict, "Host", "Mpesa", req_name, error)

        if error:
            frappe.throw(_(response["errorMessage"]), title=_("Transaction Error"))


def sanitize_mobile_number(number: str) -> str:
    """Add country code and strip leading zeroes from the phone number."""
    return "254" + str(number).lstrip("0")


def get_completed_integration_requests_info(
    reference_doctype: str, reference_docname: str, checkout_id: str
) -> tuple[list, list]:
    output_of_other_completed_requests = frappe.get_all(
        "Integration Request",
        filters={
            "name": ["!=", checkout_id],
            "reference_doctype": reference_doctype,
            "reference_docname": reference_docname,
            "status": "Completed",
        },
        pluck="output",
    )

    mpesa_receipts, completed_payments = [], []

    for out in output_of_other_completed_requests:
        out = frappe._dict(loads(out))
        item_response = out["CallbackMetadata"]["Item"]
        completed_amount = fetch_param_value(item_response, "Amount", "Name")
        completed_mpesa_receipt = fetch_param_value(
            item_response, "MpesaReceiptNumber", "Name"
        )
        completed_payments.append(completed_amount)
        mpesa_receipts.append(completed_mpesa_receipt)

    return mpesa_receipts, completed_payments


@frappe.whitelist(allow_guest=True)
def process_balance_info(**kwargs) -> None:
    """Process and store account balance information received via callback from the account balance API call."""
    account_balance_response = frappe._dict(kwargs["Result"])

    conversation_id = getattr(account_balance_response, "ConversationID", "")
    if not isinstance(conversation_id, str):
        frappe.throw(_("Invalid Conversation ID"))

    request = frappe.get_doc("Integration Request", conversation_id)

    if request.status == "Completed":
        return

    transaction_data = frappe._dict(loads(request.data))

    if account_balance_response["ResultCode"] == 0:
        try:
            result_params = account_balance_response["ResultParameters"][
                "ResultParameter"
            ]

            balance_info = fetch_param_value(result_params, "AccountBalance", "Key")
            balance_info = format_string_to_json(balance_info)

            ref_doc = frappe.get_doc(
                transaction_data.reference_doctype, transaction_data.reference_docname
            )
            ref_doc.db_set("account_balance", balance_info)

            request.handle_success(account_balance_response)
            frappe.publish_realtime(
                "refresh_mpesa_dashboard",
                doctype="Mpesa Settings",
                docname=transaction_data.reference_docname,
                user=transaction_data.owner,
            )
        except Exception:
            request.handle_failure(account_balance_response)
            frappe.log_error(
                title="Mpesa Account Balance Processing Error",
                message=account_balance_response,
            )
    else:
        request.handle_failure(account_balance_response)


def format_string_to_json(balance_info: str) -> str:
    """
    Format string to json.

    e.g: '''Working Account|KES|481000.00|481000.00|0.00|0.00'''
    => {'Working Account': {'current_balance': '481000.00',
            'available_balance': '481000.00',
            'reserved_balance': '0.00',
            'uncleared_balance': '0.00'}}
    """
    balance_dict = frappe._dict()
    for account_info in balance_info.split("&"):
        account_info = account_info.split("|")
        balance_dict[account_info[0]] = dict(
            current_balance=fmt_money(account_info[2], currency="KES"),
            available_balance=fmt_money(account_info[3], currency="KES"),
            reserved_balance=fmt_money(account_info[4], currency="KES"),
            uncleared_balance=fmt_money(account_info[5], currency="KES"),
        )
    return dumps(balance_dict)


def fetch_param_value(response: dict, key: str, key_field: str) -> str | None:
    """Fetch the specified key from list of dictionary. Key is identified via the key field."""
    for param in response:
        if param[key_field] == key:
            return param["Value"]


def create_mode_of_payment(
    gateway: str, payment_type: str = "General", company: str = None
) -> Document:
    with erpnext_app_import_guard():
        from erpnext import get_default_company

    payment_gateway_account = frappe.db.get_value(
        "Payment Gateway Account", {"payment_gateway": gateway}, ["payment_account"]
    )

    mode_of_payment = frappe.db.exists("Mode of Payment", gateway)
    if not mode_of_payment and payment_gateway_account:
        mode_of_payment = frappe.get_doc(
            {
                "doctype": "Mode of Payment",
                "mode_of_payment": gateway,
                "enabled": 1,
                "type": payment_type,
                "accounts": [
                    {
                        "doctype": "Mode of Payment Account",
                        "company": company or get_default_company(),
                        "default_account": payment_gateway_account,
                    }
                ],
            }
        )
        mode_of_payment.insert(ignore_permissions=True)

        return mode_of_payment

    return frappe.get_doc("Mode of Payment", mode_of_payment)
