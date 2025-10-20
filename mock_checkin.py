from shop_status_manager import ShopStatusManager
import time

manager = ShopStatusManager()

# Simulate members checking in
manager.check_in("ABC1234567")  # Kushagra
manager.check_in("XYZ9876543")  # Julianna

print("Current members after check-in:", manager.get_current_members())

time.sleep(2)  # wait some time

# Simulate check-out
manager.check_out("ABC1234567")

print("Current members after Kushagra checked out:", manager.get_current_members())
