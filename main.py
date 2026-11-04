#!/usr/bin/env python3
"""JobBot Android UI - Kivy + KivyMD-free, single-activity."""

import os
import threading
import traceback
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.utils import platform as kivy_platform

import jobbot_core as core
import jobbot_ai as ai_mod


PROVINCES = ["gauteng", "western cape", "kwazulu-natal", "eastern cape",
             "free state", "limpopo", "mpumalanga", "north west", "northern cape"]


def _btn(text, cb, **kw):
    b = Button(text=text, size_hint_y=None, height=dp(48), **kw)
    b.bind(on_release=lambda *_: cb())
    return b


def _scroll_label(text=""):
    lbl = Label(text=text, size_hint_y=None, halign="left", valign="top",
                text_size=(dp(300), None))
    lbl.bind(texture_size=lambda i, v: setattr(i, "height", v[1]))
    sv = ScrollView()
    sv.add_widget(lbl)
    return sv, lbl


# ==========================================================
# SEARCH SCREEN
# ==========================================================
class SearchScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.cancel_flag = threading.Event()
        root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))

        root.add_widget(Label(text="[b]JobBot AI Search[/b]", markup=True,
                              size_hint_y=None, height=dp(32)))
        self.q = TextInput(hint_text="What work are you looking for?",
                           size_hint_y=None, height=dp(48), multiline=False)
        self.prov = Spinner(text=core.load_settings().get("province", "gauteng") or "gauteng",
                            values=PROVINCES, size_hint_y=None, height=dp(48))
        root.add_widget(self.q)
        root.add_widget(self.prov)

        row = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(6))
        self.go = Button(text="Search", on_release=lambda *_: self.start())
        self.stop = Button(text="Stop", disabled=True,
                           on_release=lambda *_: self.cancel_flag.set())
        row.add_widget(self.go); row.add_widget(self.stop)
        root.add_widget(row)

        sv, self.out = _scroll_label()
        root.add_widget(sv)

        root.add_widget(_btn("Jobs list", lambda: self._go("jobs")))
        root.add_widget(_btn("Settings", lambda: self._go("settings")))
        self.add_widget(root)

    def _go(self, name):
        self.manager.current = name
        if name == "jobs":
            self.manager.get_screen("jobs").refresh()

    def start(self):
        if not core.load_master_cv():
            self._append("Import your CV in Settings first.")
            return
        self.go.disabled = True; self.stop.disabled = False
        self.out.text = ""
        self.cancel_flag.clear()
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            ai_mod.ai_job_search(self.q.text, self.prov.text.lower(),
                                 progress=self.say, cancel=self.cancel_flag)
        except Exception as e:
            core.log(traceback.format_exc())
            self.say(f"Error: {e}", level="error")
        finally:
            Clock.schedule_once(lambda *_: self._done())

    @mainthread
    def _done(self):
        self.go.disabled = False; self.stop.disabled = True
        self.manager.get_screen("jobs").refresh()

    def say(self, msg, level="info", done=False):
        Clock.schedule_once(lambda *_: self._append(msg))

    @mainthread
    def _append(self, msg):
        self.out.text += msg + "\n"


# ==========================================================
# JOBS SCREEN
# ==========================================================
class JobsScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        top = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        top.add_widget(Button(text="< Back", on_release=lambda *_: self._back()))
        top.add_widget(Button(text="Refresh", on_release=lambda *_: self.refresh()))
        top.add_widget(Button(text="Send selected", on_release=lambda *_: self._send()))
        root.add_widget(top)

        self.sv = ScrollView()
        self.list = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.list.bind(minimum_height=self.list.setter("height"))
        self.sv.add_widget(self.list)
        root.add_widget(self.sv)
        self.add_widget(root)

    def _back(self):
        self.manager.current = "search"

    def refresh(self):
        self.list.clear_widgets()
        jobs = core.list_jobs()
        if not jobs:
            self.list.add_widget(Label(text="No jobs yet. Run a search.",
                                       size_hint_y=None, height=dp(60)))
            return
        for j in jobs:
            self.list.add_widget(self._card(j))

    def _card(self, j):
        box = BoxLayout(orientation="vertical", size_hint_y=None,
                        height=dp(180), padding=dp(8), spacing=dp(3))
        box.add_widget(Label(text=f"[b]{j['title'][:60]}[/b]", markup=True,
                             size_hint_y=None, height=dp(24)))
        box.add_widget(Label(text=f"{j.get('company') or 'Unknown'} - {j.get('location') or '-'}",
                             size_hint_y=None, height=dp(20)))
        box.add_widget(Label(
            text=f"AI: {j.get('ai_score',0)} | Match: {j['match_score']} | "
                 f"Scam: {j['scam_score']} | {core.CV_MODE_LABELS.get(j.get('cv_mode') or '', '(not chosen)')}",
            size_hint_y=None, height=dp(20)))
        if j.get("ai_reason"):
            box.add_widget(Label(text=j["ai_reason"][:120],
                                 size_hint_y=None, height=dp(24)))
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(4))
        row.add_widget(Button(text="custom ats cv",
                              on_release=lambda *_: self._set_mode(j["uid"], core.CV_MODE_CUSTOM)))
        row.add_widget(Button(text="own cv",
                              on_release=lambda *_: self._set_mode(j["uid"], core.CV_MODE_OWN)))
        row.add_widget(Button(text="Deselect" if j["selected"] else "Select",
                              on_release=lambda *_: self._toggle(j["uid"])))
        row.add_widget(Button(text="Email",
                              on_release=lambda *_: self._email(j["uid"])))
        box.add_widget(row)
        return box

    def _set_mode(self, uid, mode):
        core.set_job_cv_mode(uid, mode, select=True)
        self.refresh()

    def _toggle(self, uid):
        core.toggle_job(uid)
        self.refresh()

    def _email(self, uid):
        scr = self.manager.get_screen("email")
        scr.load(uid)
        self.manager.current = "email"

    def _send(self):
        scr = self.manager.get_screen("send")
        scr.start()
        self.manager.current = "send"


# ==========================================================
# EMAIL SCREEN
# ==========================================================
class EmailScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.uid = None
        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        root.add_widget(Label(text="Edit email", size_hint_y=None, height=dp(28)))
        self.subject = TextInput(size_hint_y=None, height=dp(44), multiline=False,
                                 hint_text="Subject")
        root.add_widget(self.subject)
        self.body = TextInput(multiline=True)
        root.add_widget(self.body)
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        row.add_widget(Button(text="Save", on_release=lambda *_: self._save()))
        row.add_widget(Button(text="Regenerate", on_release=lambda *_: self._regen()))
        row.add_widget(Button(text="Back", on_release=lambda *_: self._back()))
        root.add_widget(row)
        self.add_widget(root)

    def load(self, uid):
        self.uid = uid
        result = core.ensure_job_email(uid)
        if result:
            self.subject.text, self.body.text = result
        else:
            self.subject.text = ""
            self.body.text = ""

    def _save(self):
        if self.uid:
            core.save_job_email(self.uid, self.subject.text, self.body.text)
            self._back()

    def _regen(self):
        if self.uid:
            core.ensure_job_email(self.uid, overwrite=True)
            self.load(self.uid)

    def _back(self):
        self.manager.current = "jobs"
        self.manager.get_screen("jobs").refresh()


# ==========================================================
# SEND SCREEN
# ==========================================================
class SendScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.ctrl = None
        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        root.add_widget(Label(text="Sending", size_hint_y=None, height=dp(28)))
        sv, self.out = _scroll_label()
        root.add_widget(sv)
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        row.add_widget(Button(text="Cancel", on_release=lambda *_: self._cancel()))
        row.add_widget(Button(text="Back", on_release=lambda *_: self._back()))
        root.add_widget(row)
        self.add_widget(root)

    def start(self):
        self.out.text = ""
        self.ctrl = core.SendController(progress=self.say, ai=ai_mod.AIClient())
        self.ctrl.start()

    def _cancel(self):
        if self.ctrl:
            self.ctrl.cancel()

    def _back(self):
        self.manager.current = "jobs"
        self.manager.get_screen("jobs").refresh()

    def say(self, msg, level="info", done=False):
        Clock.schedule_once(lambda *_: self._append(msg))

    @mainthread
    def _append(self, msg):
        self.out.text += msg + "\n"


# ==========================================================
# SETTINGS SCREEN
# ==========================================================
class SettingsScreen(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(4))
        root.add_widget(Label(text="Settings", size_hint_y=None, height=dp(28)))

        sv = ScrollView()
        box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6),
                        padding=dp(4))
        box.bind(minimum_height=box.setter("height"))

        self.fields = {}
        s = core.load_settings()
        keys = ["ai_api_key", "ai_base_url", "ai_model",
                "smtp_user", "smtp_pass", "smtp_server", "smtp_port",
                "adzuna_app_id", "adzuna_app_key",
                "jooble_key", "rapidapi_key", "serpapi_key",
                "send_delay_seconds", "send_delay_jitter",
                "max_emails_per_run", "default_cv_mode", "province"]
        for k in keys:
            box.add_widget(Label(text=k, size_hint_y=None, height=dp(20)))
            t = TextInput(text=str(s.get(k, "")), size_hint_y=None,
                          height=dp(42), multiline=False)
            self.fields[k] = t
            box.add_widget(t)

        box.add_widget(_btn("Save settings", self._save))
        box.add_widget(_btn("Import master CV (parses CV)", self._import_cv))
        box.add_widget(_btn("Import own CV (attached unchanged)", self._import_own_cv))
        box.add_widget(_btn("Back", lambda: setattr(self.manager, "current", "search")))
        sv.add_widget(box)
        root.add_widget(sv)
        self.add_widget(root)

    def _save(self):
        s = core.load_settings()
        for k, t in self.fields.items():
            v = t.text.strip()
            if k in ("send_delay_seconds", "send_delay_jitter",
                     "max_emails_per_run", "smtp_port"):
                try:
                    v = int(v)
                except ValueError:
                    pass
            s[k] = v
        core.save_settings(s)

    def _pick_file(self, purpose):
        """Android file picker via androidstorage4kivy; falls back to a path prompt."""
        try:
            from androidstorage4kivy import SharedStorage, Chooser
            chooser = Chooser(self._on_chosen)
            self._purpose = purpose
            chooser.choose_content("application/pdf")
            return
        except Exception:
            pass
        # fallback: ask for a path
        from kivy.uix.popup import Popup
        popup = Popup(title="Enter file path", size_hint=(0.9, 0.3))
        inner = BoxLayout(orientation="vertical")
        ti = TextInput(multiline=False, hint_text="/storage/emulated/0/...")
        inner.add_widget(ti)
        def go(*_):
            popup.dismiss()
            self._load_path(ti.text.strip(), purpose)
        inner.add_widget(Button(text="OK", size_hint_y=None, height=dp(48), on_release=go))
        popup.content = inner
        popup.open()

    def _on_chosen(self, uri, shared, *args):
        # copy from content:// uri into app storage
        try:
            from androidstorage4kivy import SharedStorage
            local = SharedStorage().copy_from_shared(uri)
            self._load_path(local, self._purpose)
        except Exception as e:
            core.log(f"picker: {e}")

    def _load_path(self, path, purpose):
        try:
            if purpose == "master":
                data = core.import_master_cv(path)
                from kivy.uix.popup import Popup
                Popup(title="Imported", content=Label(text="CV imported OK"),
                      size_hint=(0.7, 0.3)).open()
            else:
                dest = core.store_own_cv(path)
                from kivy.uix.popup import Popup
                Popup(title="Own CV saved",
                      content=Label(text=f"Stored as {dest.name}"),
                      size_hint=(0.7, 0.3)).open()
        except Exception as e:
            core.log(traceback.format_exc())
            from kivy.uix.popup import Popup
            Popup(title="Error", content=Label(text=str(e)[:200]),
                  size_hint=(0.8, 0.3)).open()

    def _import_cv(self):
        self._pick_file("master")

    def _import_own_cv(self):
        self._pick_file("own")


# ==========================================================
# APP
# ==========================================================
class JobBotApp(App):
    def build(self):
        try:
            core.init_db()
        except Exception:
            core.log(traceback.format_exc())
        sm = ScreenManager()
        sm.add_widget(SearchScreen(name="search"))
        sm.add_widget(JobsScreen(name="jobs"))
        sm.add_widget(EmailScreen(name="email"))
        sm.add_widget(SendScreen(name="send"))
        sm.add_widget(SettingsScreen(name="settings"))
        return sm

    def on_pause(self):
        return True

    def on_resume(self):
        pass


if __name__ == "__main__":
    JobBotApp().run()
