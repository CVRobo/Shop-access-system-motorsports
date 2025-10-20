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
        # normalize to lowercase for comparison
        mask = (df["member_name"].str.lower() == member_name.lower()) & (df["approved"] == False)
        idx = df[mask].index.max()
        if pd.isna(idx):
            return False
        df.at[idx, "approved"] = True
        df.to_csv(self.attendance_csv, index=False)
        return True
    def approve_all_hours(self, member_name):
        """
        Approve all unapproved hours for the given member.
        Returns total hours approved, or 0 if none found.
        """
        df = pd.read_csv(self.attendance_csv)
        mask = (df["member_name"].str.lower() == member_name.lower()) & (df["approved"] == False)
        to_approve = df[mask]

        if to_approve.empty:
            return 0.0

        total_hours = to_approve["hours"].sum()
        df.loc[mask, "approved"] = True
        df.to_csv(self.attendance_csv, index=False)
        return total_hours
    def get_current_members(self):
        return list(self.current_members.keys())
    def is_lead_of(self, lead_slack_id, member_name):
        member_row = self.members[self.members["member_name"].str.lower() == member_name.lower()]
        if member_row.empty:
            return False
        lead_id = str(member_row.iloc[0]["lead_ID"]).strip()
        return lead_id == lead_slack_id
