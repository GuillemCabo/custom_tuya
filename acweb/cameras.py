"""Cameras for the web interface: live video through go2rtc, settings, PTZ.

Two kinds:
  * iCSee/XMEye (Xiongmai) cameras listed in cameras.json: video over local RTSP, settings and PTZ
    over the local DVRIP protocol (acweb.dvrip).
  * Tuya cameras found in devices.json: settings through the Tuya cloud API; video through the
    cloud's stream URL, which needs the "IoT Video Live Stream" service on the Tuya IoT project.

go2rtc (docker-compose.yml) does the conversion to something browsers play. Streams are registered
through its API from here, so camera logins stay in gitignored files.
"""
import copy
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .dvrip import DVRIP, DVRIPError

log = logging.getLogger("acweb.cameras")
GO2RTC = os.environ.get("GO2RTC_URL", "http://127.0.0.1:8321").rstrip("/")
DETECT = os.environ.get("DETECT_URL", "http://127.0.0.1:8323").rstrip("/")
TUYA_CAMERA_CATEGORIES = {"sp", "dmsxj", "sxt", "wxsxj", "ksp", "spks"}

# ---------------------------------------------------------------------------------------------
# Settings shown in the panel for Xiongmai cameras. A field only appears when the camera has that
# config and path. kind: range | number | bool | hexbool | select | text | hm (hour+minute pair)
# ---------------------------------------------------------------------------------------------
VC, CP, CPX = "AVEnc.VideoColor.[0]", "Camera.Param.[0]", "Camera.ParamEx.[0]"
WL, ENC, OSD = "Camera.WhiteLight", "Simplify.Encode", "AVEnc.VideoWidget"
MD, HD = "Detect.MotionDetect.[0]", "Detect.HumanDetection.[0]"
ONOFF_HEX = [["0x00000000", "Off"], ["0x00000001", "On"]]
DAYS = [[d, d] for d in ("Never", "Everyday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                         "Saturday", "Sunday")]


def F(group, key, label, config, path, kind, **kw):
    return dict(group=group, key=key, label=label, config=config, path=path, kind=kind, **kw)


XM_FIELDS = [
    F("Image", "brightness", "Brightness", VC, [0, "VideoColorParam", "Brightness"], "range", min=0, max=100),
    F("Image", "contrast", "Contrast", VC, [0, "VideoColorParam", "Contrast"], "range", min=0, max=100),
    F("Image", "saturation", "Saturation", VC, [0, "VideoColorParam", "Saturation"], "range", min=0, max=100),
    F("Image", "hue", "Hue", VC, [0, "VideoColorParam", "Hue"], "range", min=0, max=100),
    F("Image", "mirror", "Mirror", CP, ["PictureMirror"], "hexbool"),
    F("Image", "flip", "Flip upside down", CP, ["PictureFlip"], "hexbool"),
    F("Image", "style", "Image style", CPX, ["Style"], "select",
      options=[["type1", "Style 1"], ["type2", "Style 2"], ["type3", "Style 3"]]),
    F("Image", "blc", "Backlight compensation", CP, ["BLCMode"], "hexbool"),
    F("Image", "flicker", "Anti-flicker (50 Hz lights)", CP, ["RejectFlicker"], "hexbool"),
    F("Image", "aesens", "Exposure sensitivity", CP, ["AeSensitivity"], "range", min=1, max=10),
    F("Image", "overexpo", "Prevent overexposure", CPX, ["PreventOverExpo"], "bool"),
    F("Image", "nightenh", "Night enhancement", CPX, ["NightEnhance"], "bool"),
    F("Image", "dnr_day", "Noise reduction (day)", CP, ["Day_nfLevel"], "range", min=0, max=5),
    F("Image", "dnr_night", "Noise reduction (night)", CP, ["Night_nfLevel"], "range", min=0, max=5),
    F("Image", "defog", "Defog", "Camera.ClearFog", [0, "enable"], "bool"),
    F("Image", "defog_lvl", "Defog level", "Camera.ClearFog", [0, "level"], "range", min=0, max=100),
    F("Image", "dis", "Image stabilisation", CPX, ["Dis"], "bool"),
    F("Image", "ldc", "Lens distortion correction", CPX, ["Ldc"], "bool"),
    F("Image", "corridor", "Corridor mode (rotate 90°)", CPX, ["CorridorMode"], "bool"),

    F("Night & light", "wl_mode", "Night vision / white light", WL, ["WorkMode"], "select", options=[
        ["Intelligent", "Smart: infrared, white light on motion"], ["Auto", "Full colour: white light at night"],
        ["Close", "Infrared only (white light off)"], ["KeepOpen", "White light always on"],
        ["Timing", "White light on a schedule"]]),
    F("Night & light", "wl_bright", "White light brightness", WL, ["Brightness"], "range", min=0, max=100),
    F("Night & light", "wl_dur", "Light stays on after motion (s)", WL, ["MoveTrigLight", "Duration"], "number",
      min=5, max=600),
    F("Night & light", "wl_lvl", "Light motion sensitivity", WL, ["MoveTrigLight", "Level"], "range", min=1, max=5),
    F("Night & light", "wl_sched", "Schedule enabled", WL, ["WorkPeriod", "Enable"], "bool"),
    F("Night & light", "wl_on", "Schedule on at", WL, [["WorkPeriod", "SHour"], ["WorkPeriod", "SMinute"]], "hm"),
    F("Night & light", "wl_off", "Schedule off at", WL, [["WorkPeriod", "EHour"], ["WorkPeriod", "EMinute"]], "hm"),
    F("Night & light", "daynight", "Colour mode", CP, ["DayNightColor"], "select", options=[
        ["0x00000000", "Auto"], ["0x00000001", "Always colour"], ["0x00000002", "Always black & white"]],
      note="Other values are set by the iCSee app's night-vision modes."),
    F("Night & light", "dncthr", "Day/night switch threshold", CP, ["DncThr"], "range", min=1, max=100),
    F("Night & light", "ircut_swap", "Swap IR-cut filter", CP, ["IrcutSwap"], "bool",
      note="Only if day and night look reversed."),

    F("Detection", "md_on", "Motion detection", MD, ["Enable"], "bool"),
    F("Detection", "md_lvl", "Motion sensitivity", MD, ["Level"], "range", min=1, max=6),
    F("Detection", "md_push", "Send alerts to the app", MD, ["EventHandler", "MessageEnable"], "bool"),
    F("Detection", "md_rec", "Record on motion", MD, ["EventHandler", "RecordEnable"], "bool"),
    F("Detection", "md_snap", "Snapshot on motion", MD, ["EventHandler", "SnapEnable"], "bool"),
    F("Detection", "md_ptz", "Move PTZ on motion", MD, ["EventHandler", "PtzEnable"], "bool"),
    F("Detection", "md_beep", "Camera beeps on motion", MD, ["EventHandler", "BeepEnable"], "bool"),
    F("Detection", "hd_on", "Human detection", HD, ["Enable"], "bool"),
    F("Detection", "hd_sens", "Human sensitivity", HD, ["Sensitivity"], "select",
      options=[[0, "Low"], [1, "Medium"], [2, "High"]]),
    F("Detection", "hd_track", "Draw box around people", HD, ["ShowTrack"], "bool"),
    F("Detection", "hd_rule", "Show detection area", HD, ["ShowRule"], "bool"),
    F("Detection", "blind", "Tamper (lens covered) alarm", "Detect.BlindDetect.[0]", ["Enable"], "bool"),

    F("Video", "main_res", "Main stream resolution", ENC, [0, "MainFormat", "Video", "Resolution"], "text",
      readonly=True),
    F("Video", "main_fps", "Main stream frame rate", ENC, [0, "MainFormat", "Video", "FPS"], "range", min=1, max=25),
    F("Video", "main_br", "Main stream bitrate (kbps)", ENC, [0, "MainFormat", "Video", "BitRate"], "number",
      min=256, max=8192),
    F("Video", "main_brc", "Main stream bitrate mode", ENC, [0, "MainFormat", "Video", "BitRateControl"], "select",
      options=[["VBR", "Variable"], ["CBR", "Constant"]]),
    F("Video", "main_q", "Main stream quality", ENC, [0, "MainFormat", "Video", "Quality"], "range", min=1, max=6),
    F("Video", "main_gop", "Main stream keyframe interval (s)", ENC, [0, "MainFormat", "Video", "GOP"], "range",
      min=1, max=12),
    F("Video", "main_audio", "Record audio", ENC, [0, "MainFormat", "AudioEnable"], "bool"),
    F("Video", "sub_res", "Substream resolution", ENC, [0, "ExtraFormat", "Video", "Resolution"], "text",
      readonly=True),
    F("Video", "sub_fps", "Substream frame rate", ENC, [0, "ExtraFormat", "Video", "FPS"], "range", min=1, max=25),
    F("Video", "sub_br", "Substream bitrate (kbps)", ENC, [0, "ExtraFormat", "Video", "BitRate"], "number",
      min=64, max=4096),
    F("Video", "sub_q", "Substream quality", ENC, [0, "ExtraFormat", "Video", "Quality"], "range", min=1, max=6),

    F("Overlay & audio", "title", "Camera name on video", OSD, [0, "ChannelTitle", "Name"], "text", maxlen=30),
    F("Overlay & audio", "show_title", "Show name", OSD, [0, "ChannelTitleAttribute", "EncodeBlend"], "bool",
      also=[[0, "ChannelTitleAttribute", "PreviewBlend"]]),
    F("Overlay & audio", "show_time", "Show date & time", OSD, [0, "TimeTitleAttribute", "EncodeBlend"], "bool",
      also=[[0, "TimeTitleAttribute", "PreviewBlend"]]),
    F("Overlay & audio", "volume", "Speaker volume", "fVideo.Volume", [0, "LeftVolume"], "range", min=0, max=100,
      also=[[0, "RightVolume"]]),

    F("System", "ntp", "Set clock from the internet (NTP)", "NetWork.NetNTP", ["Enable"], "bool"),
    F("System", "ntp_server", "NTP server", "NetWork.NetNTP", ["Server", "Name"], "text", maxlen=60),
    F("System", "dst", "Daylight saving time", "General.Location", ["DSTRule"], "select",
      options=[["On", "On"], ["Off", "Off"]]),
    F("System", "reboot_day", "Automatic reboot", "General.AutoMaintain", ["AutoRebootDay"], "select", options=DAYS),
    F("System", "reboot_hour", "Automatic reboot hour", "General.AutoMaintain", ["AutoRebootHour"], "range",
      min=0, max=23),
]
XM_FIELD = {f["key"]: f for f in XM_FIELDS}

# Factory values for the Image tab (Xiongmai defaults; sharpness/white balance are not touched).
IMAGE_DEFAULTS = {"brightness": 50, "contrast": 50, "saturation": 50, "hue": 50, "mirror": False, "flip": False,
                  "style": "type1", "blc": False, "flicker": False, "aesens": 5, "overexpo": False,
                  "nightenh": False, "dnr_day": 3, "dnr_night": 3, "defog": False, "defog_lvl": 50, "dis": False,
                  "ldc": False, "corridor": False}

# Configs offered in the raw JSON editor. Network/Wi-Fi configs are left out on purpose: a typo
# there takes the camera off the network.
XM_RAW = [CP, CPX, VC, WL, "Camera.ClearFog", ENC, MD, HD, "Detect.BlindDetect.[0]", "Detect.LossDetect.[0]",
          OSD, "fVideo.Volume", "Record.[0]", "General.General", "General.Location", "General.AutoMaintain",
          "NetWork.NetNTP", "System.TimeZone", "Uart.PTZPreset", "Uart.PTZTour"]

PTZ_COMMANDS = {"DirectionUp", "DirectionDown", "DirectionLeft", "DirectionRight", "DirectionLeftUp",
                "DirectionLeftDown", "DirectionRightUp", "DirectionRightDown", "ZoomTile", "ZoomWide",
                "FocusNear", "FocusFar"}


def dig(obj, path):
    for p in path:
        obj = obj[p]
    return obj


def plant(obj, path, value):
    dig(obj, path[:-1])[path[-1]] = value


def has_path(obj, path):
    try:
        dig(obj, path)
        return True
    except (KeyError, IndexError, TypeError):
        return False


def coerce(kind, value, original):
    if kind == "hexbool":
        return "0x00000001" if value in (True, 1, "1", "true", "0x00000001") else "0x00000000"
    if kind == "bool":
        b = value in (True, 1, "1", "true", "on")
        return int(b) if isinstance(original, int) and not isinstance(original, bool) else b
    if kind in ("range", "number") or isinstance(original, int) and not isinstance(original, bool):
        return int(float(value))
    return str(value)


class XMCamera:
    kind = "icsee"

    def __init__(self, cid, cfg):
        self.id, self.cfg = cid, cfg
        self.ip = cfg["ip"]
        self.dev = DVRIP(self.ip, cfg["user"], cfg["password"], int(cfg.get("dvrip_port", 34567)))
        self.name = cfg.get("name", cid)
        self.meta, self.error, self._stop_timer = {}, None, None

    # streams: main = full resolution, sub = small one for the grid
    def rtsp(self, stream):
        q = urllib.parse.quote
        return (f"rtsp://{self.ip}:{self.cfg.get('rtsp_port', 554)}/user={q(self.cfg['user'])}"
                f"&password={q(self.cfg['password'])}&channel=1&stream={stream}.sdp")

    def go2rtc_streams(self):
        out = {}
        for name, n in ((self.id, 0), (self.id + "_sub", 1)):
            # raw H.265 for browsers that decode it; go2rtc only starts these ffmpeg transcodes
            # when a viewer can't take the original codec
            out[name] = [self.rtsp(n), f"ffmpeg:{name}#video=h264", f"ffmpeg:{name}#audio=aac"]
        return out

    def load_meta(self):
        try:
            info = self.dev.info()
            ability = self.dev.ability()
            title = dig(self.dev.get(OSD), [0, "ChannelTitle", "Name"])
            presets = []
            try:
                presets = [dict(id=p["Id"], name=p.get("PresetName") or f"Preset {p['Id']}")
                           for p in self.dev.get("Uart.PTZPreset")[0] if p.get("PresetName")]
            except (DVRIPError, IndexError, KeyError, TypeError):
                pass
            other = ability.get("OtherFunction", {})
            self.meta = dict(model=info.get("HardWare"), firmware=info.get("SoftWareVersion"),
                             serial=info.get("SerialNo"), built=info.get("BuildTime"),
                             uptime_min=int(info.get("DeviceRunTime", "0x0"), 16),
                             ptz=bool(other.get("SupportPTZDirectionControl")),
                             tour=bool(other.get("SupportPTZTour")), presets=presets,
                             dual_lens=bool(other.get("SupportGunBallTwoSensorCamera")))
            self.name = title or self.name
            self.error = None
        except DVRIPError as e:
            self.error = str(e)
        return self.meta

    def summary(self):
        if not self.meta:
            self.load_meta()
        return dict(id=self.id, kind=self.kind, name=self.name, ip=self.ip, online=not self.error,
                    error=self.error, stream=self.id, grid_stream=self.id + "_sub", **self.meta)

    def settings(self):
        configs, errors = {}, {}
        for name in dict.fromkeys(f["config"] for f in XM_FIELDS):
            try:
                configs[name] = self.dev.get(name)
            except DVRIPError as e:
                errors[name] = str(e)
        fields = []
        for f in XM_FIELDS:
            cfg = configs.get(f["config"])
            paths = f["path"] if f["kind"] == "hm" else [f["path"]]
            if cfg is None or not all(has_path(cfg, p) for p in paths):
                continue
            value = ([dig(cfg, p) for p in paths] if f["kind"] == "hm" else dig(cfg, f["path"]))
            fields.append(dict(f, value=value))
        info = dict(self.meta)
        try:
            wifi = self.dev.get("NetWork.Wifi")
            info.update(wifi_ssid=wifi.get("SSID"), wifi_auth=wifi.get("Auth"))
        except DVRIPError:
            pass
        try:
            disks = self.dev.info("StorageInfo") or []
            parts = [p for d in disks for p in d.get("Partition", [])]
            if parts:
                total = sum(int(p.get("TotalSpace", "0x0"), 16) for p in parts)
                free = sum(int(p.get("RemainSpace", "0x0"), 16) for p in parts)
                info.update(storage=f"{free} MB free of {total} MB")
        except (DVRIPError, ValueError, TypeError):
            pass
        return dict(fields=fields, info=info, raw_configs=XM_RAW, errors=errors,
                    actions=[dict(id="synctime", label="Set camera clock from this computer"),
                             dict(id="reboot", label="Reboot camera", confirm="Reboot the camera now?")])

    def set(self, key, value):
        f = XM_FIELD.get(key)
        if not f or f.get("readonly"):
            raise ValueError(f"unknown or read-only setting {key}")
        cfg = self.dev.get(f["config"])
        new = copy.deepcopy(cfg)
        if f["kind"] == "hm":
            h, m = (int(x) for x in str(value).split(":"))
            sent = [h, m]
            plant(new, f["path"][0], h)
            plant(new, f["path"][1], m)
        else:
            sent = coerce(f["kind"], value, dig(cfg, f["path"]))
            for p in [f["path"]] + f.get("also", []):
                if has_path(new, p):
                    plant(new, p, sent)
        error = None
        try:
            self.dev.set(f["config"], new)
        except DVRIPError as e:
            error = str(e)
        time.sleep(0.3)
        back = self.dev.get(f["config"])
        reported = [dig(back, p) for p in f["path"]] if f["kind"] == "hm" else dig(back, f["path"])
        if key == "title":
            self.name = reported or self.name
        return dict(key=key, label=f["label"], sent=sent, reported=reported,
                    accepted=reported == sent and not error, error=error)

    def reset_image(self):
        """Put every Image-tab setting back to IMAGE_DEFAULTS, one write per config section."""
        by_config = {}
        for key, value in IMAGE_DEFAULTS.items():
            by_config.setdefault(XM_FIELD[key]["config"], []).append((XM_FIELD[key], value))
        results = []
        for name, items in by_config.items():
            try:
                cfg = self.dev.get(name)
            except DVRIPError:
                continue  # this camera doesn't have the section
            new, sent = copy.deepcopy(cfg), {}
            for f, value in items:
                if has_path(new, f["path"]):
                    sent[f["key"]] = coerce(f["kind"], value, dig(cfg, f["path"]))
                    plant(new, f["path"], sent[f["key"]])
            error = None
            try:
                self.dev.set(name, new)
            except DVRIPError as e:
                error = str(e)
            time.sleep(0.3)
            back = self.dev.get(name)
            for f, _ in items:
                if f["key"] in sent:
                    reported = dig(back, f["path"])
                    results.append(dict(key=f["key"], label=f["label"], sent=sent[f["key"]], reported=reported,
                                        accepted=reported == sent[f["key"]] and not error, error=error))
        return results

    def get_raw(self, name):
        if name not in XM_RAW:
            raise ValueError("config not editable here")
        return self.dev.get(name)

    def put_raw(self, name, value):
        if name not in XM_RAW:
            raise ValueError("config not editable here")
        self.dev.set(name, value)
        time.sleep(0.3)
        back = self.dev.get(name)
        if name == "Uart.PTZPreset":
            self.meta = {}
        return dict(saved=back, identical=back == value)

    def ptz(self, command, stop=False, preset=None):
        if command == "GotoPreset":
            return self.dev.ptz("GotoPreset", preset=int(preset))
        if command in ("StartTour", "StopTour"):
            return self.dev.ptz(command, preset=0, tour=0)
        if command not in PTZ_COMMANDS:
            raise ValueError("unknown PTZ command")
        if self._stop_timer:
            self._stop_timer.cancel()
        if stop:
            return self.dev.ptz(command, preset=-1)
        ret = self.dev.ptz(command)
        # safety net: stop by itself if the browser never sends the stop (closed tab, lost touch)
        self._stop_timer = threading.Timer(4, lambda: self._safe_stop(command))
        self._stop_timer.daemon = True
        self._stop_timer.start()
        return ret

    def _safe_stop(self, command):
        try:
            self.dev.ptz(command, preset=-1)
        except DVRIPError:
            pass

    def action(self, action):
        if action == "reboot":
            self.dev.operation("OPMachine", {"Action": "Reboot"})
            self.dev.close()
            self.meta = {}
            return "Camera is rebooting; video returns in about a minute."
        if action == "synctime":
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            self.dev.operation("OPTimeSetting", now)
            return f"Camera clock set to {now}."
        if action == "reset_image":
            return dict(message="Image settings reset to factory values.", results=self.reset_image())
        raise ValueError("unknown action")


# Standard Tuya camera data points: friendly names and enum labels. Unknown ones are still shown.
TUYA_LABELS = {
    "basic_nightvision": ("Night vision", {"0": "Auto", "1": "Off", "2": "On"}),
    "basic_flip": ("Flip image", None), "basic_osd": ("Time watermark", None),
    "basic_indicator": ("Status light", None), "basic_private": ("Privacy mode", None),
    "basic_wdr": ("Wide dynamic range", None), "basic_anti_flicker": ("Anti-flicker", {"0": "Off", "1": "50 Hz", "2": "60 Hz"}),
    "motion_switch": ("Motion detection", None), "motion_sensitivity": ("Motion sensitivity", {"0": "Low", "1": "Medium", "2": "High"}),
    "motion_tracking": ("Motion tracking", None), "decibel_switch": ("Sound detection", None),
    "decibel_sensitivity": ("Sound sensitivity", {"0": "Low", "1": "High"}),
    "record_switch": ("SD card recording", None), "record_mode": ("Recording mode", {"1": "On events", "2": "Continuous"}),
    "sd_format": ("Format SD card", None), "sd_storge": ("SD card (total | used | free, MB)", None),
    "sd_status": ("SD card status", None), "sd_format_state": ("Format progress", None),
    "wireless_electricity": ("Battery", None), "wireless_lowpower": ("Low-battery warning at", None),
    "wireless_powermode": ("Power source", {"0": "Battery", "1": "Mains"}), "wireless_awake": ("Awake", None),
    "doorbell_ring_exist": ("Indoor chime", None), "chime_ring_volume": ("Chime volume", None),
    "basic_device_volume": ("Speaker volume", None), "ptz_control": ("PTZ direction", None),
}
TUYA_HIDDEN = {"doorbell_pic", "initiative_message", "movement_detect_pic", "alarm_message", "doorbell_active"}


class TuyaCamera:
    """A Tuya camera reached through the Tuya cloud (settings, and video when the live-stream
    service is enabled on the IoT project)."""
    kind = "tuya"

    def __init__(self, dev, cloud):
        self.id, self.dev, self.cloud = dev["id"], dev, cloud
        self.name = dev.get("name") or dev["id"]
        self.spec, self.spec_time, self.online, self.error, self.stream_url = None, 0, None, None, None

    def _req(self, path, **kw):
        r = self.cloud.cloudrequest(path, **kw)
        if not r.get("success"):
            raise RuntimeError(r.get("msg") or "Tuya cloud error")
        return r.get("result")

    def summary(self):
        try:
            d = self._req(f"/v1.0/devices/{self.id}")
            self.online, self.error = d.get("online"), None
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
        return dict(id=self.id, kind=self.kind, name=self.name, online=bool(self.online), error=self.error,
                    model=self.dev.get("product_name"), stream=self.id if self.stream_url else None,
                    grid_stream=self.id if self.stream_url else None, ptz=False, presets=[])

    def connect(self):
        """Ask the cloud for a fresh stream URL (they expire) and hand it to go2rtc."""
        r = self.cloud.cloudrequest(f"/v1.0/devices/{self.id}/stream/actions/allocate", action="POST",
                                    post={"type": "rtsp"})
        if not r.get("success"):
            msg = r.get("msg") or "Tuya cloud error"
            if "not subscribed" in msg.lower() or "permission" in msg.lower():
                msg = ("Tuya refuses live video for this project. In the Tuya IoT console, open your "
                       "cloud project → Service API → Go to authorise, add “IoT Video Live Stream”, then retry.")
            raise RuntimeError(msg)
        self.stream_url = r["result"]["url"]
        return {self.id: [self.stream_url, f"ffmpeg:{self.id}#video=h264", f"ffmpeg:{self.id}#audio=aac"]}

    def settings(self):
        if not self.spec or time.time() - self.spec_time > 600:
            self.spec, self.spec_time = self._req(f"/v1.0/iot-03/devices/{self.id}/specification"), time.time()
        status = {s["code"]: s["value"] for s in self._req(f"/v1.0/iot-03/devices/{self.id}/status")}
        fields = []
        writable = set()
        for fn in self.spec.get("functions", []):
            writable.add(fn["code"])
            fields.append(self._field(fn, status.get(fn["code"]), readonly=False))
        for st in self.spec.get("status", []):
            if st["code"] not in writable and st["code"] not in TUYA_HIDDEN:
                fields.append(self._field(st, status.get(st["code"]), readonly=True))
        info = dict(online=self.online, category=self.dev.get("category"), product=self.dev.get("product_name"),
                    mac=self.dev.get("mac"))
        return dict(fields=fields, info=info, raw_configs=[], errors={}, actions=[])

    @staticmethod
    def _field(fn, value, readonly):
        try:
            vals = json.loads(fn.get("values") or "{}")
        except ValueError:
            vals = {}
        code, t = fn["code"], fn.get("type")
        label, names = TUYA_LABELS.get(code, (code.replace("_", " ").capitalize(), None))
        f = dict(group="Settings" if not readonly else "Status", key=code,
                 label=label, value=value, readonly=readonly, note=code)
        if t == "Boolean":
            f["kind"] = "bool"
        elif t == "Enum":
            f.update(kind="select", options=[[v, (names or {}).get(v, v)] for v in vals.get("range", [])])
            if readonly and names:
                f["value"] = names.get(value, value)
        elif t == "Integer":
            scale = 10 ** vals.get("scale", 0)
            f.update(kind="number", min=vals.get("min", 0) / scale, max=vals.get("max", 100) / scale,
                     unit=vals.get("unit", ""))
        else:
            f["kind"] = "text"
        if any(w in code for w in ("format", "reset", "reboot", "restart")):
            f["confirm"] = f"Really run “{f['label']}” on {code}? This can erase data."
        return f

    def set(self, key, value):
        fn = next((f for f in (self.spec or {}).get("functions", []) if f["code"] == key), None)
        if not fn:
            raise ValueError(f"unknown setting {key}")
        if fn.get("type") == "Boolean":
            value = value in (True, 1, "1", "true", "on")
        elif fn.get("type") == "Integer":
            value = int(float(value))
        r = self.cloud.cloudrequest(f"/v1.0/iot-03/devices/{self.id}/commands", action="POST",
                                    post={"commands": [{"code": key, "value": value}]})
        error = None if r.get("success") else (r.get("msg") or "refused")
        time.sleep(1.5)
        status = {s["code"]: s["value"] for s in self._req(f"/v1.0/iot-03/devices/{self.id}/status")}
        reported = status.get(key)
        return dict(key=key, label=key, sent=value, reported=reported, accepted=not error and reported == value,
                    error=error or (None if self.online else "camera is offline; the cloud may only apply it later"))

    def ptz(self, *a, **k):
        raise ValueError("PTZ not available for this camera")

    def action(self, action):
        raise ValueError("unknown action")


class Cameras:
    def __init__(self, cameras_file="cameras.json", devices_file="devices.json", tinytuya_file="tinytuya.json"):
        self.cams = {}
        path = Path(os.environ.get("CAMERAS", cameras_file))
        if path.exists():
            for cid, cfg in json.loads(path.read_text()).items():
                if cfg.get("kind", "icsee") == "icsee":
                    self.cams[cid] = XMCamera(cid, cfg)
        try:
            devices = json.loads(Path(os.environ.get("AC_DEVICES", devices_file)).read_text())
            tuya_cams = [d for d in devices if d.get("category") in TUYA_CAMERA_CATEGORIES]
            if tuya_cams and Path(tinytuya_file).exists():
                import tinytuya
                creds = json.loads(Path(tinytuya_file).read_text())
                cloud = tinytuya.Cloud(apiRegion=creds["apiRegion"], apiKey=creds["apiKey"],
                                       apiSecret=creds["apiSecret"], apiDeviceID=creds.get("apiDeviceID"))
                for d in tuya_cams:
                    self.cams[d["id"]] = TuyaCamera(d, cloud)
        except Exception as e:  # noqa: BLE001
            log.warning("Tuya cameras not loaded: %s", e)
        self.go2rtc_error = None

    def get(self, cid):
        cam = self.cams.get(cid)
        if not cam:
            raise KeyError(f"no camera {cid}")
        return cam

    def list(self):
        return dict(cameras=[c.summary() for c in self.cams.values()], go2rtc_error=self.go2rtc_error,
                    go2rtc_port=urllib.parse.urlparse(GO2RTC).port, detect_port=urllib.parse.urlparse(DETECT).port)

    # --- go2rtc -------------------------------------------------------------------------------
    @staticmethod
    def _go2rtc(method, path, params=None):
        url = f"{GO2RTC}{path}" + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as r:
                body = r.read()
                return json.loads(body) if body.strip().startswith(b"{") else body
        except urllib.error.HTTPError as e:
            # PUT also tries to save the stream to go2rtc.yaml, which is read-only on purpose:
            # the stream is created anyway and the 400 about the file can be ignored
            if method == "PUT" and e.code == 400:
                return None
            raise

    def register(self, streams, replace=False):
        for name, srcs in streams.items():
            if replace:
                try:
                    self._go2rtc("DELETE", "/api/streams", {"src": name})
                except urllib.error.URLError:
                    pass
            self._go2rtc("PUT", "/api/streams", [("name", name)] + [("src", s) for s in srcs])

    def sync(self):
        """Make sure go2rtc has every local camera stream (it forgets them when restarted)."""
        try:
            have = self._go2rtc("GET", "/api/streams") or {}
            for cam in self.cams.values():
                if isinstance(cam, XMCamera):
                    self.register({n: s for n, s in cam.go2rtc_streams().items() if n not in have})
            self.go2rtc_error = None
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.go2rtc_error = f"go2rtc not reachable at {GO2RTC} ({e}). Start it with: docker compose up -d"

    def connect(self, cid):
        cam = self.get(cid)
        if isinstance(cam, TuyaCamera):
            self.register(cam.connect(), replace=True)
        else:
            self.sync()
        return cam.summary()

    def start(self):
        def loop():
            while True:
                self.sync()
                time.sleep(20)
        threading.Thread(target=loop, name="go2rtc-sync", daemon=True).start()
