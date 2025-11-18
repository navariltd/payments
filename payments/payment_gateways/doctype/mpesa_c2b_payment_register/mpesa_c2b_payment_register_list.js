// Copyright (c) 2025, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings["Mpesa C2B Payment Register"] = {
  onload: function (listview) {
    // Add a custom button to the page actions (top bar)
    listview.page.add_inner_button(__("Check Transaction Status"), function () {
      frappe.prompt(
        [
          {
            label: "Mpesa Settings",
            fieldname: "mpesa_settings",
            fieldtype: "Link",
            options: "Mpesa Settings",
            reqd: 1,
          },
          {
            label: "Transaction ID",
            fieldname: "transaction_id",
            fieldtype: "Data",
            reqd: 1,
          },
          {
            label: "Remarks",
            fieldname: "remarks",
            fieldtype: "Small Text",
          },
        ],
        (values) => {
          frappe.db.get_value(
            "Mpesa Settings",
            values.mpesa_settings,
            ["initiator_name", "security_credential"],
            (settings) => {
              if (
                !settings ||
                (!settings.initiator_name && !settings.security_credential)
              ) {
                frappe.throw(
                  __(
                    "Please set the initiator name and security credential in the selected Mpesa Settings"
                  )
                );
              }

              frappe.call({
                method:
                  "payments.payment_gateways.m_pesa.api.trigger_transaction_status",
                args: {
                  mpesa_settings: values.mpesa_settings,
                  transaction_id: values.transaction_id,
                  remarks: values.remarks,
                },
                callback: (r) => {
                  const resp = r.message || {};
                  if (resp) {
                    frappe.msgprint({
                      message: __(
                        "Transaction {0} has been queued for status checking.",
                        [values.transaction_id]
                      ),
                      title: __("Transaction Queued"),
                      indicator: "green",
                    });
                  }
                },
                error: (err) => {
                  frappe.msgprint({
                    message: __(
                      "Failed to request status for transaction {0}: {1}",
                      [values.transaction_id, err.message]
                    ),
                    title: __("Request Error"),
                    indicator: "red",
                  });
                },
              });
            }
          );
        },
        __("Transaction Status Query"),
        __("Submit")
      );
    });
  },

  refresh: function (listview) {
    frappe.realtime.on("mpesa_transaction_status", function (data) {
      frappe.msgprint({
        message: __(data.message),
        title: data.status === "success" ? "Success" : "Error",
        indicator: data.status === "success" ? "green" : "red",
      });
      if (data.status === "success" && data.doc_name) {
        listview.refresh();
      }
    });
  },
};
