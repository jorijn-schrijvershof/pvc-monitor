"""
Tests for alerting.py — run from the backend/ directory:
    pytest test_alerting.py -v
"""
import os
import smtplib
import ssl
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, call
import alerting
from settings import ConfigError


def html_body(msg) -> str:
    """Decode the HTML part of a MIMEMultipart message."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            return payload.decode("utf-8") if payload else ""
    return ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_snap(pvc_name: str, days_old: int) -> dict:
    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {
        "pvc_name":     pvc_name,
        "pv_name":      f"pv-{pvc_name}",
        "snapshot_id":  f"snap-{pvc_name}-001",
        "description":  "",
        "created":      created,
        "datastore":    "datastore1",
        "storage_class": "vsphere-sc",
        "volume_handle": f"uuid-{pvc_name}",
    }


def snap_scan(snaps, complete=True, problems=()) -> dict:
    """The shape vcenter_client.get_snapshots() returns."""
    return {"snapshots": list(snaps), "complete": complete, "problems": list(problems)}


def make_disk(**over) -> dict:
    disk = {
        "fcd_id": "fcd-1", "fcd_name": "disk", "datastore": "ds1", "capacity_gb": 10.0,
        "category": "candidate", "cns_cluster_id": None, "cns_cluster": None,
        "attached": "no", "age_hours": 100.0, "too_young": False, "actionable": True,
    }
    return {**disk, **over}


def make_scan(disks, complete=True, problems=(), clusters=None) -> dict:
    counts = {k: 0 for k in ("in_use", "inconsistent", "orphan_known_to_cns", "foreign", "candidate", "unknown")}
    for d in disks:
        counts[d["category"]] += 1
    counts["actionable"] = sum(1 for d in disks if d["actionable"])
    return {
        "disks": disks, "counts": counts, "complete": complete, "problems": list(problems),
        "cns": "ok", "clusters": clusters or [{"name": "a", "csi_cluster_id": "id-a"}],
        "min_age_hours": 24, "scanned_at": "2026-01-01T00:00:00+00:00",
    }


SMTP_ENV = {
    "SMTP_HOST":               "smtp.example.com",
    "SMTP_PORT":               "587",
    "SMTP_USE_TLS":            "true",
    "SMTP_FROM":               "monitor@example.com",
    "SMTP_USER":               "",
    "SMTP_PASSWORD":           "",
    "ALERT_EMAIL_TO":          "ops@example.com",
    "ALERT_SNAPSHOT_AGE_DAYS": "7",
    "ALERT_CHECK_INTERVAL_HOURS": "24",
}

@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level state between tests."""
    alerting._state.update(last_check=None, last_alert=None, last_count=None, last_error=None)
    yield


# ── is_configured ─────────────────────────────────────────────────────────────

class TestIsConfigured:
    def test_all_required_set(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            assert alerting.is_configured() is True

    def test_missing_smtp_host(self):
        env = {**SMTP_ENV, "SMTP_HOST": ""}
        with patch.dict(os.environ, env, clear=False):
            assert alerting.is_configured() is False

    def test_missing_smtp_from(self):
        env = {**SMTP_ENV, "SMTP_FROM": ""}
        with patch.dict(os.environ, env, clear=False):
            assert alerting.is_configured() is False

    def test_missing_alert_to(self):
        env = {**SMTP_ENV, "ALERT_EMAIL_TO": ""}
        with patch.dict(os.environ, env, clear=False):
            assert alerting.is_configured() is False

    @pytest.mark.parametrize("value", ["0.5", "abc"])
    def test_invalid_interval_refused(self, value):
        env = {**SMTP_ENV, "ALERT_CHECK_INTERVAL_HOURS": value}
        with patch.dict(os.environ, env, clear=False):
            with pytest.raises(ConfigError, match="ALERT_CHECK_INTERVAL_HOURS"):
                alerting.get_config()

    def test_multiple_recipients(self):
        env = {**SMTP_ENV, "ALERT_EMAIL_TO": "a@b.com, c@d.com"}
        with patch.dict(os.environ, env, clear=False):
            cfg = alerting.get_config()
            assert cfg["alert_to"] == ["a@b.com", "c@d.com"]


# ── _compose_email ────────────────────────────────────────────────────────────

class TestComposeEmail:
    def _cfg(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            return alerting.get_config()

    def test_subject_with_old_snaps(self):
        snaps = [make_snap("my-pvc", 10)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "1 snapshot older than 7 days" in msg["Subject"]

    def test_subject_plural(self):
        snaps = [make_snap("pvc1", 10), make_snap("pvc2", 15)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "2 snapshots older than 7 days" in msg["Subject"]

    def test_subject_test_no_snaps(self):
        msg = alerting._compose_email(self._cfg(), [], test=True)
        assert "Test alert" in msg["Subject"]
        assert "no old snapshots" in msg["Subject"].lower()

    def test_body_contains_pvc_name(self):
        snaps = [make_snap("important-pvc", 20)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "important-pvc" in html_body(msg)

    def test_body_contains_age(self):
        snaps = [make_snap("pvc1", 12)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "12d" in html_body(msg)

    def test_recipients_set(self):
        snaps = [make_snap("pvc1", 10)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "ops@example.com" in msg["To"]

    def test_from_set(self):
        snaps = [make_snap("pvc1", 10)]
        msg = alerting._compose_email(self._cfg(), snaps)
        assert "monitor@example.com" in msg["From"]

    def test_markup_in_inventory_names_is_escaped(self):
        snap = {**make_snap("pvc1", 10), "datastore": "<b>x</b>", "pvc_name": "<b>x</b>"}
        body = html_body(alerting._compose_email(self._cfg(), [snap]))
        assert "&lt;b&gt;x&lt;/b&gt;" in body
        assert "<b>x</b>" not in body

    def test_unknown_age_reported_as_unknown(self):
        snap = {**make_snap("pvc1", 10), "created": None}
        msg = alerting._compose_email(self._cfg(), [snap])
        assert "unknown age" in msg["Subject"]
        assert "unknown" in html_body(msg)


class TestComposeOrphanEmail:
    def _cfg(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            return alerting.get_config()

    def test_markup_in_inventory_names_is_escaped(self):
        disk = make_disk(fcd_name="<b>x</b>", datastore="<b>x</b>", cns_cluster_id="<b>x</b>")
        scan = make_scan([disk], complete=False, problems=["Datastore <b>x</b>: First Class Disks could not be listed"],
                         clusters=[{"name": "<b>x</b>", "csi_cluster_id": None}])
        body = html_body(alerting._compose_orphan_email(self._cfg(), scan))
        assert "&lt;b&gt;x&lt;/b&gt;" in body
        assert "<b>x</b>" not in body

    def test_only_actionable_disks_listed_and_no_deletion_wording(self):
        scan = make_scan([make_disk(fcd_id="act-1"), make_disk(fcd_id="unk-1", category="unknown", actionable=False)])
        msg = alerting._compose_orphan_email(self._cfg(), scan)
        body = html_body(msg)
        assert "act-1" in body and "unk-1" not in body
        assert "1 disk not claimed" in msg["Subject"]
        for wording in ("safe to delete", "wasted", "orphaned"):
            assert wording not in body.lower() and wording not in msg["Subject"].lower()

    def test_incomplete_scan_stated_in_subject_and_body(self):
        scan = make_scan([make_disk()], complete=False, problems=["Datastore ds2: First Class Disks could not be listed"])
        msg = alerting._compose_orphan_email(self._cfg(), scan)
        assert "INCOMPLETE" in msg["Subject"]
        body = html_body(msg)
        assert "scan was incomplete" in body and "ds2" in body

    def test_test_mail_for_incomplete_scan_does_not_claim_none(self):
        scan = make_scan([], complete=False, problems=["x"])
        msg = alerting._compose_orphan_email(self._cfg(), scan, test=True)
        body = html_body(msg)
        assert "INCOMPLETE" in msg["Subject"] and "no disks" not in msg["Subject"]
        assert "scan was incomplete" in body and "No disk currently meets" not in body

    def test_test_mail_with_undecided_disks_does_not_claim_none(self):
        scan = make_scan([make_disk(category="unknown", actionable=False)])
        msg = alerting._compose_orphan_email(self._cfg(), scan, test=True)
        body = html_body(msg)
        assert "1 disks undecided" in msg["Subject"] and "no disks" not in msg["Subject"]
        assert "could not be categorised" in body and "No disk currently meets" not in body


# ── _send_email ───────────────────────────────────────────────────────────────

class TestSendEmail:
    def _cfg(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            return alerting.get_config()

    def _msg(self):
        return alerting._compose_email(self._cfg(), [make_snap("pvc1", 10)])

    def test_starttls_path(self):
        cfg = {**self._cfg(), "smtp_port": 587, "smtp_use_tls": True}
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__ = lambda s: mock_server
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            alerting._send_email(cfg, self._msg())
        mock_server.starttls.assert_called_once()

    def test_ssl_path_uses_smtp_ssl(self):
        cfg = {**self._cfg(), "smtp_port": 465}
        mock_server = MagicMock()
        with patch("smtplib.SMTP_SSL") as mock_ssl, patch("smtplib.SMTP") as mock_plain:
            mock_ssl.return_value.__enter__ = lambda s: mock_server
            mock_ssl.return_value.__exit__ = MagicMock(return_value=False)
            alerting._send_email(cfg, self._msg())
        mock_ssl.assert_called_once()
        mock_plain.assert_not_called()
        context = mock_ssl.call_args.kwargs["context"]
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

    def test_starttls_uses_verifying_context(self):
        cfg = {**self._cfg(), "smtp_port": 587, "smtp_use_tls": True}
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__ = lambda s: mock_server
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            alerting._send_email(cfg, self._msg())
        context = mock_server.starttls.call_args.kwargs["context"]
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

    def test_credentials_never_sent_without_tls(self):
        cfg = {**self._cfg(), "smtp_port": 587, "smtp_use_tls": False, "smtp_user": "user@example.com"}
        with patch("smtplib.SMTP") as mock_smtp, \
             patch.dict(os.environ, {"SMTP_PASSWORD": "secret"}, clear=False):
            with pytest.raises(RuntimeError, match="without TLS"):
                alerting._send_email(cfg, self._msg())
        mock_smtp.assert_not_called()

    def test_login_called_when_credentials_set(self):
        cfg = {**self._cfg(), "smtp_port": 587, "smtp_use_tls": True, "smtp_user": "user@example.com"}
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp, \
             patch.dict(os.environ, {"SMTP_PASSWORD": "secret"}, clear=False):
            mock_smtp.return_value.__enter__ = lambda s: mock_server
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            alerting._send_email(cfg, self._msg())
        mock_server.login.assert_called_once_with("user@example.com", "secret")

    def test_no_login_when_no_credentials(self):
        cfg = {**self._cfg(), "smtp_port": 587, "smtp_use_tls": True, "smtp_user": ""}
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as mock_smtp, \
             patch.dict(os.environ, {"SMTP_PASSWORD": ""}, clear=False):
            mock_smtp.return_value.__enter__ = lambda s: mock_server
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            alerting._send_email(cfg, self._msg())
        mock_server.login.assert_not_called()


# ── check_and_alert ───────────────────────────────────────────────────────────

class TestCheckAndAlert:
    def test_returns_not_configured_when_smtp_missing(self):
        env = {**SMTP_ENV, "SMTP_HOST": ""}
        with patch.dict(os.environ, env, clear=False):
            result = alerting.check_and_alert()
        assert result["sent"] is False
        assert result["reason"] == "not_configured"

    def test_state_last_error_set_when_not_configured(self):
        env = {**SMTP_ENV, "SMTP_HOST": ""}
        with patch.dict(os.environ, env, clear=False):
            alerting.check_and_alert()
        assert alerting._state["last_error"] == "SMTP not configured"

    def test_no_old_snaps_does_not_send(self):
        snaps = [make_snap("pvc1", 3)]  # 3 days — under 7-day threshold
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert()
        assert result["sent"] is False
        assert result["reason"] == "no_old_snapshots"
        mock_send.assert_not_called()

    def test_old_snaps_triggers_email(self):
        snaps = [make_snap("pvc1", 10)]  # 10 days — over threshold
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert()
        assert result["sent"] is True
        assert result["count"] == 1
        assert "ops@example.com" in result["recipients"]
        mock_send.assert_called_once()

    def test_only_old_snaps_included(self):
        snaps = [make_snap("old-pvc", 10), make_snap("new-pvc", 2)]
        captured = {}
        def capture_send(cfg, msg):
            captured["msg"] = msg
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email", side_effect=capture_send):
            result = alerting.check_and_alert()
        assert result["count"] == 1
        body = html_body(captured["msg"])
        assert "old-pvc" in body
        assert "new-pvc" not in body

    def test_force_sends_even_with_no_old_snaps(self):
        snaps = [make_snap("pvc1", 2)]  # under threshold
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert(force=True)
        assert result["sent"] is True
        mock_send.assert_called_once()

    def test_force_with_no_snaps_at_all_sends_test_email(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan([])), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert(force=True)
        assert result["sent"] is True
        mock_send.assert_called_once()

    def test_smtp_error_captured(self):
        snaps = [make_snap("pvc1", 10)]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email", side_effect=smtplib.SMTPException("Connection refused")):
            result = alerting.check_and_alert()
        assert result["sent"] is False
        assert result["reason"] == "smtp_error"
        # The detail goes to the log, not to API responses.
        assert "Connection refused" not in result["error"]
        assert alerting._state["last_error"] == result["error"]

    def test_fetch_error_captured(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", side_effect=RuntimeError("vCenter unavailable")):
            result = alerting.check_and_alert()
        assert result["sent"] is False
        assert result["reason"] == "fetch_error"
        assert "vCenter unavailable" not in result["error"]
        assert alerting._state["last_error"] == result["error"]

    def test_snapshot_with_unknown_age_is_alerted(self):
        snaps = [{**make_snap("pvc1", 1), "created": "not-a-date"}]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert()
        assert result["sent"] is True and result["count"] == 1
        mock_send.assert_called_once()

    def test_last_check_updated(self):
        assert alerting._state["last_check"] is None
        snaps = [make_snap("pvc1", 2)]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email"):
            alerting.check_and_alert()
        assert alerting._state["last_check"] is not None

    def test_last_alert_updated_on_send(self):
        snaps = [make_snap("pvc1", 10)]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email"):
            alerting.check_and_alert()
        assert alerting._state["last_alert"] is not None

    def test_last_alert_not_updated_when_no_send(self):
        snaps = [make_snap("pvc1", 2)]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email"):
            alerting.check_and_alert()
        assert alerting._state["last_alert"] is None

    def test_custom_threshold(self):
        env = {**SMTP_ENV, "ALERT_SNAPSHOT_AGE_DAYS": "14"}
        snaps = [make_snap("pvc1", 10)]  # 10 days — under 14-day threshold
        with patch.dict(os.environ, env, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert()
        assert result["sent"] is False
        mock_send.assert_not_called()


# ── get_status ────────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_status_when_configured(self):
        with patch.dict(os.environ, SMTP_ENV, clear=False):
            status = alerting.get_status()
        assert status["configured"] is True
        assert status["age_threshold_days"] == 7
        assert status["check_interval_hours"] == 24.0
        assert "ops@example.com" in status["alert_to"]

    def test_status_when_not_configured(self):
        env = {**SMTP_ENV, "SMTP_HOST": ""}
        with patch.dict(os.environ, env, clear=False):
            status = alerting.get_status()
        assert status["configured"] is False

    def test_status_reflects_state(self):
        snaps = [make_snap("pvc1", 10)]
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=snap_scan(snaps)), \
             patch("alerting._send_email"):
            alerting.check_and_alert()
            status = alerting.get_status()
        assert status["last_check"] is not None
        assert status["last_alert"] is not None
        assert status["last_old_snap_count"] == 1


# ── check_and_alert_orphans ───────────────────────────────────────────────────

class TestCheckAndAlertOrphans:
    def run(self, scan=None, error=None, force=False):
        side = {"side_effect": error} if error else {"return_value": scan}
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_orphaned_fcds", **side), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert_orphans(force=force)
        return result, mock_send

    def test_actionable_disk_sends(self):
        result, send = self.run(make_scan([make_disk()]))
        assert result["sent"] is True and result["count"] == 1
        send.assert_called_once()

    def test_non_actionable_disks_do_not_send(self):
        result, send = self.run(make_scan([make_disk(category="foreign", actionable=False)]))
        assert result == {"sent": False, "reason": "no_actionable", "count": 0, "complete": True, "undecided": 0}
        send.assert_not_called()
        assert alerting._state_orphans["last_error"] is None

    def test_incomplete_scan_is_never_none_found(self):
        result, send = self.run(make_scan([], complete=False, problems=["x"]))
        assert result["reason"] == "incomplete"
        assert alerting._state_orphans["last_error"] == alerting._INCOMPLETE
        send.assert_not_called()

    def test_undecided_disks_are_never_none_found(self):
        result, send = self.run(make_scan([make_disk(category="unknown", actionable=False)]))
        assert result["reason"] == "undecided"
        assert alerting._state_orphans["last_error"] == alerting._UNDECIDED
        send.assert_not_called()

    def test_incomplete_scan_with_actionable_disk_sends_and_records(self):
        result, send = self.run(make_scan([make_disk()], complete=False, problems=["x"]))
        assert result["sent"] is True and result["complete"] is False
        assert alerting._state_orphans["last_error"] == alerting._INCOMPLETE

    def test_cluster_failure_records_the_cluster_text(self):
        from settings import Unavailable
        text = "Kubernetes cluster 'b' could not be read completely. No result is produced; see the server log."
        result, send = self.run(error=Unavailable(text))
        assert result["reason"] == "fetch_error" and result["error"] == text
        assert alerting._state_orphans["last_error"] == text
        send.assert_not_called()

    def test_unexpected_failure_hides_detail(self):
        result, _ = self.run(error=RuntimeError("secret-host.internal"))
        assert "secret-host" not in result["error"]
        assert alerting._state_orphans["last_error"] == alerting._FETCH_ERROR


# ── Incomplete snapshot scans ─────────────────────────────────────────────────

class TestIncompleteSnapshotScan:
    def run(self, scan, force=False):
        with patch.dict(os.environ, SMTP_ENV, clear=False), \
             patch("vcenter_client.get_snapshots", return_value=scan), \
             patch("alerting._send_email") as mock_send:
            result = alerting.check_and_alert(force=force)
        return result, mock_send

    def test_incomplete_scan_is_never_no_old_snapshots(self):
        result, send = self.run(snap_scan([make_snap("pvc1", 2)], complete=False, problems=["Disk x: snapshots could not be read"]))
        assert result["reason"] == "incomplete" and result["complete"] is False
        assert alerting._state["last_error"] == alerting._SNAPSHOTS_INCOMPLETE
        send.assert_not_called()

    def test_test_mail_for_incomplete_scan_does_not_claim_none(self):
        _, send = self.run(snap_scan([], complete=False, problems=["Disk x: snapshots could not be read"]), force=True)
        msg = send.call_args.args[1]
        body = html_body(msg)
        assert "INCOMPLETE" in msg["Subject"] and "no old snapshots" not in msg["Subject"].lower()
        assert "scan was incomplete" in body and "Disk x" in body and "no snapshots older" not in body

    def test_alert_for_incomplete_scan_says_so(self):
        result, send = self.run(snap_scan([make_snap("pvc1", 10)], complete=False, problems=["p"]))
        assert result["sent"] is True and result["complete"] is False
        assert "INCOMPLETE" in send.call_args.args[1]["Subject"]


# ── Repeated failures of scheduled runs ──────────────────────────────────────

class TestScheduledFailures:
    def run(self, results, threshold="3", send_error=None):
        """Feed scheduled runs with the given check results; return the failure e-mails sent."""
        sent = []
        checks = iter(results)

        def check():
            result = next(checks)
            alerting._update(alerting._state, last_error=result.get("error"))
            return result

        def send(cfg, msg):
            if send_error:
                raise send_error
            sent.append(msg)

        with patch.dict(os.environ, {**SMTP_ENV, "ALERT_FAILURE_THRESHOLD": threshold}, clear=False), \
             patch("alerting._send_email", side_effect=send):
            for _ in results:
                alerting._scheduled_run(check, alerting._state, "Snapshot age check")
        return sent

    FAIL = {"sent": False, "reason": "fetch_error", "error": alerting._FETCH_ERROR, "count": 0}
    INCOMPLETE = {"sent": False, "reason": "incomplete", "count": 0, "complete": False}
    OK = {"sent": False, "reason": "no_old_snapshots", "count": 0, "complete": True}

    def test_one_mail_at_the_threshold_then_silence(self):
        sent = self.run([self.FAIL, self.INCOMPLETE, self.FAIL, self.FAIL, self.FAIL])
        assert len(sent) == 1
        assert "failing or incomplete (3 runs in a row)" in sent[0]["Subject"]
        assert alerting._FETCH_ERROR in html_body(sent[0])

    def test_success_resets_and_rearms(self):
        sent = self.run([self.FAIL, self.FAIL, self.OK, self.FAIL, self.FAIL, self.FAIL, self.OK, self.FAIL])
        assert len(sent) == 1  # two failures, reset, three failures -> one mail
        assert alerting._state["failures"] == 1 and alerting._state["failure_alerted"] is False

    def test_threshold_one(self):
        assert len(self.run([self.FAIL], threshold="1")) == 1

    def test_failed_send_is_retried_on_the_next_failing_run(self):
        self.run([self.FAIL, self.FAIL, self.FAIL], send_error=smtplib.SMTPException("x"))
        assert alerting._state["failure_alerted"] is False
        assert len(self.run([self.FAIL])) == 1

    def test_threshold_must_be_at_least_one(self):
        with patch.dict(os.environ, {**SMTP_ENV, "ALERT_FAILURE_THRESHOLD": "0"}, clear=False):
            with pytest.raises(ConfigError, match="ALERT_FAILURE_THRESHOLD"):
                alerting.get_config()


def test_state_reads_and_writes_take_the_lock():
    """get_status and state updates wait while another thread holds the state lock."""
    import threading

    with patch.dict(os.environ, SMTP_ENV, clear=False):
        with alerting._state_lock:
            threads = [threading.Thread(target=alerting.get_status),
                       threading.Thread(target=alerting._update, args=(alerting._state,), kwargs={"last_count": 5})]
            for t in threads:
                t.start()
                t.join(0.2)
                assert t.is_alive()
            assert alerting._state["last_count"] is None
        for t in threads:
            t.join()
    assert alerting._state["last_count"] == 5
