"""Code caches distinguish identity and release their retained keys on clear."""

import gc
import unittest
import weakref

from callnav.cache import CodeIdentityMap, CodeIdentitySet


class CodeIdentityCacheTests(unittest.TestCase):
    def test_code_identity_caches_keep_equal_code_objects_separate(self):
        first = compile('value = 1', '/same/module.py', 'exec')
        second = compile('value = 1', '/same/module.py', 'exec')
        self.assertIsNot(first, second)
        self.assertEqual(first, second)
        mapping = CodeIdentityMap()
        mapping[first] = 'first'
        mapping[second] = 'second'
        self.assertEqual(len(mapping), 2)
        self.assertEqual(mapping[first], 'first')
        self.assertEqual(mapping[second], 'second')
        codes = CodeIdentitySet()
        codes.add(first)
        codes.add(second)
        self.assertEqual(len(codes), 2)
        codes.discard(first)
        self.assertNotIn(first, codes)
        self.assertIn(second, codes)


    def test_code_identity_caches_never_hash_or_compare_keys(self):
        class Key:
            def __hash__(self):
                raise AssertionError('code key was hashed')

            def __eq__(self, other):
                raise AssertionError('code key was compared')

        key, other = Key(), Key()
        mapping = CodeIdentityMap()
        mapping[key] = None
        self.assertIn(key, mapping)
        self.assertIsNone(mapping.get(key, 'missing'))
        self.assertIsNone(mapping.setdefault(key, 'replacement'))
        self.assertEqual(mapping.setdefault(other, 'other'), 'other')
        self.assertEqual(len(mapping), 2)
        self.assertIs(list(mapping)[0], key)
        self.assertIsNone(mapping.pop(key))
        self.assertEqual(mapping.pop(key, 'missing'), 'missing')
        with self.assertRaises(KeyError):
            mapping[key]
        with self.assertRaises(KeyError):
            mapping.pop(key)
        del mapping[other]
        self.assertEqual(len(mapping), 0)
        codes = CodeIdentitySet()
        codes.add(key)
        self.assertIn(key, codes)
        self.assertNotIn(other, codes)
        self.assertIs(next(iter(codes)), key)
        codes.discard(key)
        self.assertNotIn(key, codes)


    def test_code_identity_caches_retain_keys_until_cleared(self):
        for cache in (CodeIdentityMap(), CodeIdentitySet()):
            with self.subTest(cache=type(cache).__name__):
                code = compile('value = 1', '/retained/module.py', 'exec')
                reference = weakref.ref(code)
                if isinstance(cache, CodeIdentityMap):
                    cache[code] = 'value'
                else:
                    cache.add(code)
                del code
                gc.collect()
                self.assertIsNotNone(reference())
                cache.clear()
                gc.collect()
                self.assertIsNone(reference())



if __name__ == "__main__":
    unittest.main()
