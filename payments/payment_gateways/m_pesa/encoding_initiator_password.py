import frappe
import base64
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
import os


def load_certificate_path(cert_url: str, sandbox: bool) -> str:
    """
    Try loading certificate from File doctype.
    If missing, fall back to local /public_certs folder.
    Returns the absolute file path of the certificate.
    """
    try:
        if cert_url and frappe.db.exists("File", {"file_url": cert_url}):
            file_doc = frappe.get_doc("File", {"file_url": cert_url})
            return file_doc.get_full_path()
    except Exception:
        pass

    base_dir = os.path.dirname(__file__)
    filename = "SandboxCertificate.cer" if sandbox else "ProductionCertificate.cer"
    local_path = os.path.join(base_dir, "public_certs", filename)

    if os.path.exists(local_path):
        return local_path

    raise FileNotFoundError(f"M-Pesa certificate not found: {cert_url}")


def generate_security_credential(
    initiator_password: str, sandbox: bool, cert_url: str = ""
) -> str:
    """
    Encrypts the initiator password using the uploaded M-Pesa public key certificate
    following M-Pesa's security credential generation requirements.
    """
    try:
        full_path = load_certificate_path(cert_url, sandbox)

        with open(full_path, "rb") as cert_file:
            cert_data = cert_file.read()

            if b"BEGIN CERTIFICATE" in cert_data:
                cert = x509.load_pem_x509_certificate(cert_data)
            else:
                cert = x509.load_der_x509_certificate(cert_data)

            public_key = cert.public_key()

        encrypted = public_key.encrypt(
            initiator_password.encode("utf-8"), padding.PKCS1v15()
        )

        return base64.b64encode(encrypted).decode("utf-8")

    except Exception as e:
        frappe.log_error("Security Credential Generation Error", str(e))
        raise frappe.ValidationError(f"Error generating security credential: {str(e)}")


def get_security_credential(settings):
    """Return the correct SecurityCredential based on sandbox/production and user settings."""
    certs = frappe.get_single("Mpesa Public Key Certificate")

    cert_url = (
        certs.sandbox_certificate if settings.sandbox else certs.production_certificate
    )
    initiator_password = settings.get_password("initiator_password", "") or ""

    saved_credential = settings.get_password("security_credential")
    if saved_credential:
        return saved_credential

    generated_cred = generate_security_credential(
        initiator_password=initiator_password,
        sandbox=settings.sandbox,
        cert_url=cert_url,
    )

    return generated_cred
