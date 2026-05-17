import json
import tempfile
import unittest
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from core.db import AccountModel, OutlookAccountModel
from services.chatgpt_mailbox_backfill import backfill_chatgpt_microsoft_mailboxes


class ChatGPTMailboxBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.root = Path(self.tmp_dir.name)
        self.engine = create_engine(f"sqlite:///{self.root / 'test.db'}")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)

    def test_backfill_imports_outlook_rows_and_links_chatgpt_accounts(self):
        export_file = self.root / "Free_demo_邮箱.txt"
        export_file.write_text(
            "demo@outlook.com----mail-pass----client-a----refresh-a\n",
            encoding="utf-8",
        )

        with Session(self.engine) as session:
            session.add(
                AccountModel(
                    platform="chatgpt",
                    email="demo@outlook.com",
                    password="chatgpt-pass",
                    extra_json="{}",
                )
            )
            session.commit()

            result = backfill_chatgpt_microsoft_mailboxes(
                file_paths=[str(export_file)],
                session=session,
            )
            session.commit()

            account = session.exec(
                select(AccountModel).where(AccountModel.email == "demo@outlook.com")
            ).one()
            extra = account.get_extra()
            outlook_account = session.exec(
                select(OutlookAccountModel).where(OutlookAccountModel.email == "demo@outlook.com")
            ).one()

        self.assertTrue(result["ok"])
        self.assertEqual(result["outlook_pool"]["created"], 1)
        self.assertEqual(result["chatgpt_accounts"]["matched"], 1)
        self.assertEqual(result["chatgpt_accounts"]["linked"], 1)
        self.assertEqual(extra["mail_provider"], "microsoft")
        self.assertEqual(extra["mailbox_account"]["email"], "demo@outlook.com")
        self.assertEqual(extra["mailbox_account"]["account_id"], str(outlook_account.id))
        self.assertEqual(extra["mailbox_account"]["extra"]["client_id"], "client-a")
        self.assertEqual(extra["mailbox_account"]["extra"]["refresh_token"], "refresh-a")

    def test_backfill_preserves_existing_snapshot_by_default(self):
        export_file = self.root / "卡密导出_20260517.txt"
        export_file.write_text(
            "demo@outlook.com----mail-pass----client-a----refresh-a\n",
            encoding="utf-8",
        )
        original_snapshot = {
            "email": "demo@outlook.com",
            "account_id": "existing-id",
            "extra": {"provider": "microsoft", "refresh_token": "existing-refresh"},
        }

        with Session(self.engine) as session:
            session.add(
                AccountModel(
                    platform="chatgpt",
                    email="demo@outlook.com",
                    password="chatgpt-pass",
                    extra_json=json.dumps({"mailbox_account": original_snapshot}, ensure_ascii=False),
                )
            )
            session.commit()

            result = backfill_chatgpt_microsoft_mailboxes(
                file_paths=[str(export_file)],
                session=session,
            )
            session.commit()

            account = session.exec(
                select(AccountModel).where(AccountModel.email == "demo@outlook.com")
            ).one()
            extra = account.get_extra()

        self.assertTrue(result["ok"])
        self.assertEqual(result["chatgpt_accounts"]["matched"], 1)
        self.assertEqual(result["chatgpt_accounts"]["linked"], 0)
        self.assertEqual(result["chatgpt_accounts"]["skipped_existing_snapshot"], 1)
        self.assertEqual(extra["mailbox_account"], original_snapshot)


if __name__ == "__main__":
    unittest.main()
