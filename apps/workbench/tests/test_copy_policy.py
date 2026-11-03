import unittest

from workbench.copy_policy import is_protected_copy_name, require_editable_copy


class CopyPolicyTests(unittest.TestCase):
    def test_chinese_protection_markers_are_detected(self):
        for marker in ('保护', '锁定', '勿改', '只读', '留档'):
            with self.subTest(marker=marker):
                self.assertTrue(is_protected_copy_name('人像-' + marker + '-v2'))

    def test_english_markers_are_case_insensitive_and_require_word_boundaries(self):
        protected = ('Protected', 'LOCKED', 'readonly', 'Read-only', 'read only',
                     'do-not-edit', 'Do Not Edit')
        for marker in protected:
            with self.subTest(marker=marker):
                self.assertTrue(is_protected_copy_name('Portrait ' + marker + ' v2'))
        for name in ('unprotected', 'Unlocked', 'readonlyish', 'do-not-editor', 'xlocked'):
            with self.subTest(name=name):
                self.assertFalse(is_protected_copy_name(name))

    def test_regular_working_copy_names_remain_editable(self):
        for name in ('TRLR', 'Warm portrait', 'Edit 01', ''):
            with self.subTest(name=name):
                self.assertFalse(is_protected_copy_name(name))
                self.assertEqual(require_editable_copy({'copy_name': name}), name)

    def test_protected_copy_is_rejected_and_non_string_name_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'protected'):
            require_editable_copy({'copy_name': '勿改-交付版'})
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            require_editable_copy({'copy_name': None})


if __name__ == '__main__':
    unittest.main()
