"""AC-1 spike: subscribe to WDAC/App Control block events and dump them as XML.

Subscribes (push-callback) to the two channels that carry App Control block
events and writes every matching event verbatim (EvtRender XML) to one file
per event, so the exact per-build field names (PolicyGUID vs PolicyID, etc.)
can be pinned for the AC-3 event forwarder.

  - Microsoft-Windows-CodeIntegrity/Operational : 3077 (enforce block),
    3076 (audit block), 3033, 3089 (per-signature info)
  - Microsoft-Windows-AppLocker/MSI and Script  : 8028/8029 (scripts/MSI),
    8039/8040 (packaged/MSIX apps)

Run AS SYSTEM to match the future service context, e.g.:
  PsExec64.exe -accepteula -s -i C:\\spike\\python-embed\\python.exe ^
      C:\\spike\\scripts\\spike-evt-subscribe.py --out C:\\spike\\artifacts\\events

--replay uses EvtSubscribeStartAtOldestRecord to re-deliver historical events
(plan B: harvest blocks that happened while the listener could not run).

Deliberately imports only the stdlib plus win32evtlog so the allow rules
needed to run it under an enforced policy stay minimal.
"""

import argparse
import getpass
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import win32evtlog

CHANNEL_DEFAULT_EVENTS = {
    "Microsoft-Windows-CodeIntegrity/Operational": (3033, 3076, 3077, 3089),
    "Microsoft-Windows-AppLocker/MSI and Script": (8028, 8029, 8039, 8040),
}

EVENT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"


def build_xpath(event_ids):
    if not event_ids:
        return "*"
    clauses = " or ".join("EventID=%d" % e for e in event_ids)
    return "*[System[(%s)]]" % clauses


def parse_ids(xml_text):
    """Best-effort (EventID, EventRecordID) extraction; never raises."""
    try:
        root = ET.fromstring(xml_text)
        system = root.find(EVENT_NS + "System")
        event_id = system.findtext(EVENT_NS + "EventID", default="unknown")
        record_id = system.findtext(EVENT_NS + "EventRecordID", default="0")
        return event_id.strip(), record_id.strip()
    except Exception:
        return "unparsed", "0"


class Listener:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.count = 0
        self.errors = 0
        self._handles = []  # keep subscription handles alive

    def callback(self, action, context, event):
        # Exceptions must never propagate into pywin32's callback thunk.
        try:
            if action == win32evtlog.EvtSubscribeActionError:
                self.errors += 1
                print("[%s] subscription error callback (context=%r)"
                      % (time.strftime("%H:%M:%S"), context), flush=True)
                return
            xml_text = win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml)
            event_id, record_id = parse_ids(xml_text)
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            name = "%s_%s_%s.xml" % (stamp, event_id, record_id)
            name = re.sub(r'[^A-Za-z0-9_.-]', "_", name)
            path = os.path.join(self.out_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(xml_text)
            self.count += 1
            print("[%s] EventID=%s RecordID=%s (channel context=%r) -> %s"
                  % (time.strftime("%H:%M:%S"), event_id, record_id, context, name),
                  flush=True)
        except Exception as exc:  # noqa: BLE001 - must swallow everything
            self.errors += 1
            print("callback failure: %r" % (exc,), flush=True)

    def subscribe(self, channel, event_ids, replay):
        flags = (win32evtlog.EvtSubscribeStartAtOldestRecord if replay
                 else win32evtlog.EvtSubscribeToFutureEvents)
        xpath = build_xpath(event_ids)
        handle = win32evtlog.EvtSubscribe(
            channel,
            flags,
            None,                    # SignalEvent (unused: push model)
            Callback=self.callback,
            Context=channel,
            Query=xpath,
        )
        self._handles.append(handle)
        print("subscribed: channel=%r mode=%s query=%s"
              % (channel, "replay-oldest" if replay else "future", xpath),
              flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Dump App Control block events as rendered XML (AC-1 spike).")
    parser.add_argument("--channels", nargs="+",
                        default=list(CHANNEL_DEFAULT_EVENTS),
                        help="Event channels to subscribe to "
                             "(default: CodeIntegrity/Operational + AppLocker MSI and Script)")
    parser.add_argument("--events", default=None,
                        help="Comma-separated EventIDs applied to ALL channels "
                             "(default: per-channel block-event sets); 'all' = no filter")
    parser.add_argument("--out", default=os.path.join(".", "events"),
                        help="Directory for one-XML-per-event output (default .\\events)")
    parser.add_argument("--replay", action="store_true",
                        help="Deliver historical events too (EvtSubscribeStartAtOldestRecord)")
    parser.add_argument("--duration", type=float, default=None,
                        help="Seconds to run (default: until Ctrl+C)")
    args = parser.parse_args(argv)

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    print("user=%s pid=%d python=%s" % (getpass.getuser(), os.getpid(), sys.version.split()[0]))
    print("out=%s" % out_dir, flush=True)

    listener = Listener(out_dir)
    for channel in args.channels:
        if args.events is None:
            event_ids = CHANNEL_DEFAULT_EVENTS.get(channel, ())
        elif args.events.strip().lower() == "all":
            event_ids = ()
        else:
            event_ids = tuple(int(t) for t in args.events.split(",") if t.strip())
        try:
            listener.subscribe(channel, event_ids, args.replay)
        except Exception as exc:  # access denied / unknown channel etc.
            print("FAILED to subscribe to %r: %r" % (channel, exc), flush=True)

    if not listener._handles:
        print("no subscriptions established; exiting", flush=True)
        return 2

    deadline = time.monotonic() + args.duration if args.duration else None
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    print("done: %d event(s) written, %d callback error(s)"
          % (listener.count, listener.errors), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
