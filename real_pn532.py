# real_pn532.py
import time
import board
import busio
from adafruit_pn532.i2c import PN532_I2C

class RealPN532:
    def __init__(self, debug=False):
        i2c = busio.I2C(board.SCL, board.SDA)
        # PN532_I2C will find the device on the i2c bus
        self.pn532 = PN532_I2C(i2c, debug=debug)
        # initialize the PN532
        self.pn532.SAM_configuration()
        # optional: adjust the read timeout you want in read_passive_target
        self.timeout = 0.5

    def read_passive_target(self):
        """
        Returns UID string on card seen, e.g. "04A1B2C3D4" (uppercase, no separators),
        or None if no card seen within timeout.
        """
        uid = self.pn532.read_passive_target(timeout=self.timeout)
        if uid is None:
            return None
        # uid is a bytearray like b'\x04\xa1\xb2\xc3\xd4'
        uid_str = "".join("{:02X}".format(b) for b in uid)
        return uid_str

    def close(self):
        # No special cleanup required in most cases; method provided for symmetry
        pass
