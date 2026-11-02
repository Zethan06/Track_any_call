"""Fresh static bindings must not execute application attribute machinery."""

import inspect
import unittest
from unittest import mock

from callnav.collector import _UNKNOWN, _UNSET, _attribute_value


class StaticAttributeTests(unittest.TestCase):
    def test_ordinary_methods_do_not_need_inspect_dispatch(self):
        class Parent:
            def method(self):
                pass

            @staticmethod
            def static():
                pass

            @classmethod
            def class_method(cls):
                pass

        class Child(Parent):
            pass

        instance = Child()
        with mock.patch.object(inspect, 'getattr_static',
                               side_effect=AssertionError('redundant static lookup')):
            self.assertIs(_attribute_value(instance, 'method'), vars(Parent)['method'])
            self.assertIs(_attribute_value(instance, 'static'), vars(Parent)['static'].__func__)
            self.assertIs(_attribute_value(instance, 'class_method'),
                          vars(Parent)['class_method'].__func__)

    def test_instance_class_and_base_mutations_are_read_fresh(self):
        def first():
            pass

        def second():
            pass

        class Left:
            entry = first

        class Right:
            entry = second

        class Child(Left):
            pass

        instance = Child()
        self.assertIs(_attribute_value(instance, 'entry'), first)
        instance.entry = second
        self.assertIs(_attribute_value(instance, 'entry'), second)
        del instance.entry
        Left.entry = second
        self.assertIs(_attribute_value(instance, 'entry'), second)
        Left.entry = first
        Child.__bases__ = (Right,)
        self.assertIs(_attribute_value(instance, 'entry'), second)
        del Right.entry
        self.assertIs(_attribute_value(instance, 'entry'), _UNKNOWN)

    def test_hooks_descriptors_and_shadowed_dict_keep_conservative_results(self):
        def target():
            pass

        class Hook:
            entry = target

            def __getattribute__(self, name):
                raise AssertionError('application getter executed')

        def forbidden(*args):
            raise AssertionError('application descriptor executed')

        class Descriptor:
            entry = property(forbidden)

        class Shadow:
            entry = target
            __dict__ = property(forbidden)

        class Slots:
            __slots__ = ('entry',)

        descriptor = Descriptor()
        vars(descriptor)['entry'] = target
        shadow = Shadow()
        shadow.entry = lambda: None
        slots = Slots()
        slots.entry = target
        self.assertIs(_attribute_value(Hook(), 'entry'), _UNKNOWN)
        self.assertIs(_attribute_value(descriptor, 'entry'), _UNKNOWN)
        self.assertIs(_attribute_value(shadow, 'entry'), target)
        self.assertIs(_attribute_value(slots, 'entry'), _UNKNOWN)

    def test_private_inspect_sentinel_and_false_class_do_not_change_lookup(self):
        class Ordinary:
            pass

        instance = Ordinary()
        instance.entry = inspect._sentinel
        self.assertIs(_attribute_value(instance, 'entry'), _UNKNOWN)
        instance.__dict__['__class__'] = dict
        instance.entry = len
        self.assertIs(_attribute_value(instance, 'entry'), len)
        self.assertIs(_attribute_value(instance, 'missing'), _UNKNOWN)

    def test_private_fallback_marker_stored_by_application_is_not_absent(self):
        class Parent:
            def entry(self):
                pass

        class Child(Parent):
            entry = _UNSET

        class Getter(Parent):
            __getattribute__ = _UNSET

        instance = Parent()
        instance.entry = _UNSET
        self.assertIs(_attribute_value(Child(), 'entry'), _UNSET)
        self.assertIs(_attribute_value(instance, 'entry'), _UNSET)
        self.assertIs(_attribute_value(Getter(), 'entry'), _UNKNOWN)
