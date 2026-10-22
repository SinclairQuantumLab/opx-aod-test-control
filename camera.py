"""Spinnaker acquisition. Temporary ROI, frame rate and controls are restored on exit."""

import importlib
import json
import numpy as np


class Camera:
    def __init__(self, settings, roi=None, fps=60, latest=False):
        self.settings, self.roi, self.fps = settings, roi, fps
        self.latest = latest
        self.device = self.cameras = self.system = self.nodes = None
        self.initialized = self.acquiring = False
        self.restore = []
        self.metadata = {"restore_errors": []}

    def set_node(self, nodes, name, value, kind):
        node = kind(nodes.GetNode(name))
        old = node.GetIntValue() if kind is self.p.CEnumerationPtr else node.GetValue()
        self.restore.append((nodes, name, kind, old))
        if kind is self.p.CEnumerationPtr:
            node.SetIntValue(node.GetEntryByName(value).GetValue())
        else:
            node.SetValue(value)

    def __enter__(self):
        self.p = p = importlib.import_module("PySpin")
        self.system = p.System.GetInstance()
        self.cameras = self.system.GetCameras()
        try:
            self.device = self.cameras.GetBySerial(self.settings["serial"])
            model = p.CStringPtr(self.device.GetTLDeviceNodeMap().GetNode("DeviceModelName")).GetValue()
            if model != self.settings["model"]:
                raise RuntimeError(f"Unexpected camera: {model}")
            self.device.Init()
            self.initialized = True
            self.nodes = nodes = self.device.GetNodeMap()
            self.metadata["before"] = {
                name: p.CValuePtr(nodes.GetNode(name)).ToString()
                for name in ("Width", "Height", "OffsetX", "OffsetY", "ExposureTime", "Gain", "PixelFormat")
            }
            if self.roi is not None:
                x, y, width, height = self.roi
                for name, value in (("OffsetX",0),("OffsetY",0),("Width",width),("Height",height),("OffsetX",x),("OffsetY",y)):
                    self.set_node(nodes, name, value, p.CIntegerPtr)
            stream = self.device.GetTLStreamNodeMap()
            handling = "NewestOnly" if self.latest else "OldestFirst"
            for name, value in (("StreamMode","LWF"),("StreamBufferHandlingMode",handling)):
                self.set_node(stream, name, value, p.CEnumerationPtr)
            for name, value in (("GainAuto","Off"),("ExposureAuto","Off"),("AcquisitionFrameRateAuto","Off"),
                                ("TriggerMode","Off"),("AcquisitionMode","Continuous"),("PixelFormat","Mono8")):
                self.set_node(nodes, name, value, p.CEnumerationPtr)
            for name in ("ExposureTime", "Gain"):
                self.set_node(nodes, name, float(self.metadata["before"][name]), p.CFloatPtr)
            self.set_node(nodes, "AcquisitionFrameRateEnable", True, p.CBooleanPtr)
            maximum = p.CFloatPtr(nodes.GetNode("AcquisitionFrameRate")).GetMax()
            self.set_node(nodes, "AcquisitionFrameRate", min(self.fps, maximum), p.CFloatPtr)
            self.metadata.update(roi=self.roi, requested_fps=self.fps, maximum_fps=maximum,
                                 configured_fps=p.CFloatPtr(nodes.GetNode("AcquisitionFrameRate")).GetValue())
            self.device.BeginAcquisition()
            self.acquiring = True
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def frame(self):
        image = self.device.GetNextImage(2000)
        try:
            if image.IsIncomplete():
                raise RuntimeError(f"Incomplete camera frame: {image.GetImageStatus()}")
            return image.GetNDArray().copy(), {"frame_id": int(image.GetFrameID()), "timestamp_ns": int(image.GetTimeStamp())}
        finally:
            image.Release()

    def frames(self, count, discard=2):
        for _ in range(discard):
            self.frame()
        collected = [self.frame() for _ in range(count)]
        return np.stack([item[0] for item in collected]), [item[1] for item in collected]

    def __exit__(self, kind, value, traceback):
        try:
            if self.acquiring:
                self.device.EndAcquisition()
            for nodes, name, cast, old in reversed(self.restore):
                try:
                    node = cast(nodes.GetNode(name))
                    if cast is self.p.CEnumerationPtr:
                        node.SetIntValue(old)
                    else:
                        node.SetValue(old)
                except Exception as exc:
                    self.metadata["restore_errors"].append({"node": name, "error": str(exc)})
            node = nodes = None
            self.restore.clear()
            self.nodes = None
            if self.initialized:
                self.device.DeInit()
        finally:
            self.device = None
            self.cameras.Clear()
            self.system.ReleaseInstance()


def capture(settings, directory):
    """One unscaled image for the CW experiment."""
    import tifffile
    from PIL import Image

    with Camera(settings, fps=3) as camera:
        frames, timestamps = camera.frames(1)
        pixels = frames[0]
    tifffile.imwrite(directory / "frame.tiff", pixels)
    Image.fromarray(pixels).save(directory / "frame.png")
    metadata = dict(camera.metadata, **timestamps[0], peak=int(pixels.max()), saturated_pixels=int((pixels == 255).sum()))
    (directory / "camera.json").write_text(json.dumps(metadata, indent=2))
    return metadata
