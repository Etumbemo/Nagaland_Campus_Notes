try:
    from app import mailer, main
except ModuleNotFoundError:
    from backend.app import mailer, main


class RecordingSMTP:
    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self):
        pass

    def login(self, _username, _password):
        pass

    def send_message(self, message):
        self.message = message


def test_institute_approval_email_uses_configured_smtp(monkeypatch):
    smtp = RecordingSMTP("smtp.example.test", 587, 15)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "no-reply@example.test")
    monkeypatch.setenv("SMTP_FROM_NAME", "Campus-notes")
    monkeypatch.setenv("SMTP_USE_TLS", "true")
    monkeypatch.setattr(mailer.smtplib, "SMTP", lambda *args, **kwargs: smtp)

    mailer.send_institute_approval_email(
        "staff@example.test",
        "A Staff Member",
        "Example College",
    )

    assert smtp.message["To"] == "staff@example.test"
    assert smtp.message["Subject"] == "Your Campus-notes institute account is approved"
    assert "Example College has been approved" in smtp.message.get_content()


def test_institute_approval_email_requires_smtp_configuration(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM_EMAIL", raising=False)

    try:
        mailer.send_institute_approval_email(
            "staff@example.test",
            "A Staff Member",
            "Example College",
        )
    except RuntimeError as error:
        assert "SMTP_HOST" in str(error)
    else:
        raise AssertionError("Missing SMTP configuration should prevent email delivery")


def test_approving_institute_sends_confirmation_email(monkeypatch):
    target = {
        "id": "institute-1",
        "email": "staff@example.test",
        "name": "A Staff Member",
        "role": "institute",
        "approval_status": "pending",
        "institute_name": "Example College",
    }
    approved = {**target, "approval_status": "approved"}
    queried_users = iter((target, approved))
    sent = []

    class RecordingDB:
        def commit(self):
            pass

    monkeypatch.setattr(main, "query_one", lambda *_args: next(queried_users))
    monkeypatch.setattr(main, "execute", lambda *_args: 1)
    monkeypatch.setattr(
        main,
        "send_institute_approval_email",
        lambda *args: sent.append(args),
    )

    result = main.update_institute_approval(
        "institute-1",
        main.InstituteApprovalUpdate(approval_status="approved"),
        db=RecordingDB(),
        user={"id": "admin-1", "role": "super_admin"},
    )

    assert sent == [("staff@example.test", "A Staff Member", "Example College")]
    assert result["approval_email_sent"] is True
