import pandas as pd
from datetime import datetime

class ShopStatusManager:
    def __init__(self, members_csv="members.csv", attendance_csv="attendance.csv"):
        self.members_csv = members_csv
        self.attendance_csv = attendance_csv
        self.members = pd.read_csv(self.members_csv, dtype=str)
        self.current_members = {}  # {member_name: check_in_time}

        # Create attendance CSV if it doesn't exist
        try:
            pd.read_csv(self.attendance_csv)
        except FileNotFoundError:
            df = pd.DataFrame(columns=["card_uid", "member_name", "check_in", "check_out", "hours", "approved"])
            df.to_csv(self.attendance_csv, index=False)

    def check_in(self, card_uid):
        row = self.members[self.members["card_uid"] == card_uid]
        if row.empty:
            return None  # unknown card

        member_name = row.iloc[0]["member_name"]
        if member_name in self.current_members:
            # Already checked in, treat as checkout
            return self.check_out(card_uid)

        check_in_time = datetime.now()
        self.current_members[member_name] = check_in_time
        lead_slack_id = row.iloc[0]["lead_slack_id"]
        return {
            "action": "check_in",
            "member": member_name,
            "time": check_in_time,
            "lead": lead_slack_id
        }

    def check_out(self, card_uid):
        row = self.members[self.members["card_uid"] == card_uid]
        if row.empty:
            return None

        member_name = row.iloc[0]["member_name"]
        if member_name not in self.current_members:
            return None  # not checked in

        check_in_time = self.current_members.pop(member_name)
        check_out_time = datetime.now()
        duration_hours = round((check_out_time - check_in_time).total_seconds() / 3600, 2)
        lead_slack_id = row.iloc[0]["lead_slack_id"]

        # Append to attendance CSV
        df = pd.read_csv(self.attendance_csv)
        df = pd.concat([
            df,
            pd.DataFrame([{
                "card_uid": card_uid,
                "member_name": member_name,
                "check_in": check_in_time,
                "check_out": check_out_time,
                "hours": duration_hours,
                "approved": False
            }])
        ])
        df.to_csv(self.attendance_csv, index=False)

        return {
            "action": "check_out",
            "member": member_name,
            "duration": duration_hours,
            "lead": lead_slack_id
        }

    def approve_hours(self, member_name):
        df = pd.read_csv(self.attendance_csv)
        # Find last entry for the member that is not approved yet
        idx = df[(df["member_name"] == member_name) & (df["approved"] == False)].index.max()
        if pd.isna(idx):
            return False
        df.at[idx, "approved"] = True
        df.to_csv(self.attendance_csv, index=False)
        return True

    def get_current_members(self):
        return list(self.current_members.keys())
