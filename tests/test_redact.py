from trends.redact import redact_emails


def test_personal_email_is_redacted():
    text = "#50031 someone.name@gmail.com (3 comments)"
    assert redact_emails(text) == "#50031 [email] (3 comments)"


def test_support_address_is_kept():
    assert redact_emails("email support@supabase.com") == "email support@supabase.com"


def test_text_without_email_is_unchanged():
    assert redact_emails("JWT issued at future") == "JWT issued at future"
