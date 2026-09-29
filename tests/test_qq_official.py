"""The button-callback patch, driven through fake adapter classes.

The patch wraps the constructor of AstrBot's built-in adapter class, so
every test hands ``install_callbacks`` a fresh stand-in class instead and
builds adapters from it the way AstrBot's platform manager does. What is
asserted is what the connection and the chat would see: whether the
identify carries the interaction intent, whether a tap is acknowledged and
answered, and that an adapter the patch does not recognise is left exactly
as AstrBot built it. The real gateway (the intent accepted, the three-second
acknowledgement) needs a server; see the ticket's field test.

``qq_official`` is imported through the plugin package, as ``main`` does,
and needs botpy for its structure check; the module is skipped without it.
"""

import asyncio
import functools
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import botpy  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    botpy = None

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT.parent))

if botpy is not None:
    from astrbot_plugin_zmdlog import qq_official
    from astrbot_plugin_zmdlog.qq_official import Chat, Click

INTERACTION = 1 << 26
# What AstrBot's adapter subscribes to with groups and private chats on.
BASE_INTENTS = 1 << 25 | 1 << 30
DATA = "/zmdlog 账号 usr_a"


class FakeApi:
    """The client's ``api``: records acknowledgements in ``log``."""

    def __init__(self, log: list, *, ack_error: Exception | None = None) -> None:
        self._log = log
        self._ack_error = ack_error

    async def on_interaction_result(self, interaction_id: str, code: int):
        self._log.append(("ack", interaction_id, code))
        if self._ack_error is not None:
            raise self._ack_error


class FakeClient:
    def __init__(self, log: list, **api) -> None:
        self.intents = BASE_INTENTS
        self.api = FakeApi(log, **api)


def adapter_class(log: list | None = None, *, shape=None, **api):
    """A stand-in for ``QQOfficialPlatformAdapter``, built the same way.

    ``shape`` edits the client after the stand-in's own constructor ran, the
    way a future AstrBot might build it differently.
    """

    log = [] if log is None else log

    class Adapter:
        def __init__(self, platform_config, platform_settings, event_queue):
            self.config = platform_config
            self.client = FakeClient(log, **api)
            if shape is not None:
                shape(self)

        def meta(self):
            return SimpleNamespace(id=self.config["id"])

    return Adapter


def build(cls):
    return cls({"id": "qq-1"}, {}, asyncio.Queue())


def tap(data: object = DATA, *, scene: str = "group", kind: int = 11):
    """An ``INTERACTION_CREATE`` as botpy hands it to the client."""

    return SimpleNamespace(
        id="itx-1",
        type=kind,
        event_id="INTERACTION_CREATE:e-1",
        data=SimpleNamespace(resolved=SimpleNamespace(button_data=data)),
        group_openid="G1" if scene == "group" else None,
        group_member_openid="M1" if scene == "group" else None,
        user_openid="U1" if scene == "c2c" else None,
    )


@unittest.skipIf(botpy is None, "botpy is not installed")
class CallbackPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log: list = []
        self.logger = mock.Mock()
        self.installed: list = []

    def tearDown(self) -> None:
        for callbacks in self.installed:
            callbacks.remove()

    def _install(self, cls, *, accepts=lambda data: True, expand=False):
        async def on_click(click):
            self.log.append(("click", click))

        callbacks = qq_official.install_callbacks(
            on_click,
            accepts=accepts,
            expand_active=lambda: expand,
            logger=self.logger,
            adapter_cls=cls,
        )
        if callbacks is not None:
            self.installed.append(callbacks)
        return callbacks

    def _tap(self, adapter, interaction=None) -> None:
        asyncio.run(adapter.client.on_interaction_create(interaction or tap()))

    def _assert_untouched(self, adapter) -> None:
        self.assertEqual(adapter.client.intents, BASE_INTENTS)
        self.assertFalse(hasattr(adapter.client, "on_interaction_create"))

    def test_a_patched_adapter_subscribes_its_connection_to_taps(self) -> None:
        cls = adapter_class(self.log)
        before = build(cls)

        callbacks = self._install(cls)
        after = build(cls)

        self.assertEqual(after.client.intents, BASE_INTENTS | INTERACTION)
        self.assertTrue(callbacks.live(Chat(bot=after.client)))
        # A connection opened before the patch never asked for taps, so its
        # buttons must keep filling the command in.
        self._assert_untouched(before)
        self.assertFalse(callbacks.live(Chat(bot=before.client)))

    def test_a_tap_is_acknowledged_first_then_answered_in_its_chat(self) -> None:
        cls = adapter_class(self.log)
        self._install(cls)
        adapter = build(cls)
        for scene, chat, origin, sender in (
            (
                "group",
                Chat(
                    bot=adapter.client,
                    group_openid="G1",
                    event_id="INTERACTION_CREATE:e-1",
                ),
                "qq-1:GroupMessage:G1",
                "M1",
            ),
            (
                "c2c",
                Chat(
                    bot=adapter.client,
                    user_openid="U1",
                    event_id="INTERACTION_CREATE:e-1",
                ),
                "qq-1:FriendMessage:U1",
                "U1",
            ),
        ):
            with self.subTest(scene=scene):
                self.log.clear()

                self._tap(adapter, tap(scene=scene))

                self.assertEqual(
                    self.log,
                    [
                        ("ack", "itx-1", 0),
                        ("click", Click(chat, DATA, origin, sender)),
                    ],
                )

    def test_a_tap_that_is_not_ours_is_left_alone(self) -> None:
        cls = adapter_class(self.log)
        self._install(cls, accepts=lambda data: data == DATA)
        adapter = build(cls)
        for label, interaction in (
            ("another button's data", tap("/other 账号 usr_a")),
            ("no data", tap(None)),
            ("not a message button", tap(kind=12)),
            ("a guild channel", tap(scene="guild")),
        ):
            with self.subTest(label):
                self._tap(adapter, interaction)

                self.assertEqual(self.log, [])

    def test_a_failed_acknowledgement_still_answers_and_logs_its_type(self) -> None:
        cls = adapter_class(self.log, ack_error=RuntimeError("a body with secrets"))
        self._install(cls)
        adapter = build(cls)

        self._tap(adapter)

        self.assertEqual([entry[0] for entry in self.log], ["ack", "click"])
        logged = repr(self.logger.method_calls)
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("secrets", logged)
        self.assertNotIn("usr_a", logged)

    def test_a_failed_answer_logs_its_type_and_never_the_data(self) -> None:
        cls = adapter_class(self.log)

        async def broken(click):
            raise ValueError(click.data)

        callbacks = qq_official.install_callbacks(
            broken,
            accepts=lambda data: True,
            expand_active=lambda: False,
            logger=self.logger,
            adapter_cls=cls,
        )
        self.installed.append(callbacks)
        adapter = build(cls)

        self._tap(adapter)

        logged = repr(self.logger.method_calls)
        self.assertIn("ValueError", logged)
        self.assertNotIn("usr_a", logged)

    def test_an_adapter_of_another_shape_is_left_as_astrbot_built_it(self) -> None:
        def no_client(adapter):
            del adapter.client

        def odd_intents(adapter):
            adapter.client.intents = "all"

        def no_acknowledgement(adapter):
            adapter.client.api = SimpleNamespace()

        for label, shape in (
            ("no client", no_client),
            ("intents that are no number", odd_intents),
            ("an api that cannot acknowledge", no_acknowledgement),
        ):
            with self.subTest(label):
                self.logger.reset_mock()
                cls = adapter_class(self.log, shape=shape)
                callbacks = self._install(cls)

                adapter = build(cls)

                client = getattr(adapter, "client", None)
                self.assertFalse(hasattr(client, "on_interaction_create"))
                if client is not None and isinstance(client.intents, int):
                    self.assertEqual(client.intents, BASE_INTENTS)
                self.assertFalse(callbacks.live(Chat(bot=client)))
                self.logger.warning.assert_called_once()

    def test_expand_enabled_keeps_the_patch_off_and_says_why(self) -> None:
        cls = adapter_class(self.log)
        callbacks = self._install(cls, expand=True)

        adapter = build(cls)

        self._assert_untouched(adapter)
        self.assertFalse(callbacks.live(Chat(bot=adapter.client)))
        (warning,) = self.logger.warning.call_args_list
        self.assertIn("qqoffice_expand", warning.args[0])
        self.assertIn("README", warning.args[0])

    def test_an_astrbot_that_answers_taps_itself_is_left_alone(self) -> None:
        def subscribed(adapter):
            adapter.client.intents |= INTERACTION

        async def native(interaction):
            return None

        def handled(adapter):
            adapter.client.on_interaction_create = native

        for label, shape in (("subscribed", subscribed), ("handled", handled)):
            with self.subTest(label):
                cls = adapter_class(self.log, shape=shape)
                callbacks = self._install(cls)

                adapter = build(cls)

                self.assertFalse(callbacks.live(Chat(bot=adapter.client)))
                if label == "handled":
                    self.assertIs(adapter.client.on_interaction_create, native)
                    self.assertEqual(adapter.client.intents, BASE_INTENTS)
                else:
                    self.assertFalse(
                        hasattr(adapter.client, "on_interaction_create")
                    )
                self.logger.warning.assert_not_called()

    def test_a_class_the_patch_cannot_wrap_installs_nothing(self) -> None:
        class NoConstructor:
            pass

        self.assertIsNone(self._install(NoConstructor))
        self.assertNotIn("__init__", vars(NoConstructor))
        self.logger.warning.assert_called_once()

    def test_removal_restores_the_constructor_and_quiets_live_taps(self) -> None:
        cls = adapter_class(self.log)
        original = cls.__init__
        callbacks = self._install(cls)
        live = build(cls)

        callbacks.remove()
        self._tap(live)

        self.assertIs(cls.__init__, original)
        self._assert_untouched(build(cls))
        self.assertEqual(self.log, [])
        self.assertFalse(callbacks.live(Chat(bot=live.client)))

    def test_a_reload_repoints_live_connections_to_the_new_load(self) -> None:
        cls = adapter_class(self.log)
        original = cls.__init__
        first = self._install(cls)
        adapter = build(cls)

        answered = []

        async def reloaded(click):
            answered.append(click.data)

        second = qq_official.install_callbacks(
            reloaded,
            accepts=lambda data: True,
            expand_active=lambda: False,
            logger=self.logger,
            adapter_cls=cls,
        )
        self.installed.append(second)
        # The earlier load's teardown, arriving late, takes nothing away.
        first.remove()
        self._tap(adapter)

        self.assertEqual(answered, [DATA])
        self.assertTrue(second.live(Chat(bot=adapter.client)))
        self.assertFalse(first.live(Chat(bot=adapter.client)))
        # One wrapper around the real constructor, never one per load.
        self.assertIs(cls.__init__.__wrapped__, original)
        self.assertEqual(build(cls).client.intents, BASE_INTENTS | INTERACTION)

    def test_another_plugins_wrapper_on_top_survives_a_reload(self) -> None:
        cls = adapter_class(self.log)
        first = self._install(cls)
        ours = cls.__init__
        built_by_other = []

        # Another plugin wraps the constructor the usual way, copying our
        # wrapper's attributes onto its own.
        @functools.wraps(ours)
        def other(adapter, *args, **kwargs):
            ours(adapter, *args, **kwargs)
            built_by_other.append(adapter)

        cls.__init__ = other
        first.remove()
        self._install(cls)
        build(cls)

        # Its wrapper still runs: ours went on top of it, not under it.
        self.assertEqual(len(built_by_other), 1)
        self.assertIs(cls.__init__.__wrapped__, other)

    def test_an_adapter_or_botpy_the_patch_does_not_know_installs_nothing(
        self,
    ) -> None:
        for label, target in (
            ("no adapter class", "_builtin_adapter_class"),
            ("a botpy that cannot take taps", "_botpy_takes_taps"),
        ):
            with self.subTest(label):
                self.logger.reset_mock()
                with mock.patch.object(qq_official, target, return_value=None):
                    callbacks = qq_official.install_callbacks(
                        lambda click: None,
                        accepts=lambda data: True,
                        expand_active=lambda: False,
                        logger=self.logger,
                        adapter_cls=(
                            None
                            if target == "_builtin_adapter_class"
                            else adapter_class()
                        ),
                    )

                self.assertIsNone(callbacks)
                self.logger.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
