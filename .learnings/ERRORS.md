## [ERR-20260515-001] pytest-temp-permissions

**Logged**: 2026-05-15T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
Pytest failed in this plugin when its default temp/cache directories were inside the plugin tree.

### Error
```text
PermissionError: [WinError 5] Access is denied: 'C:\\Users\\65168\\.claude\\plugins\\local\\self-improve\\.pytest-tmp'
PermissionError: [WinError 5] Access is denied: 'C:\\tmp\\self-improve-pytest-tmp'
```

### Context
- Command attempted: `pytest tests/test_inject_context.py tests/test_review_worker.py tests/test_feedback_collector.py`
- The sandbox user can read the plugin tree but cannot reliably create or delete pytest temp/cache paths there.
- `C:\\Users\\65168\\AppData\\Local\\Temp\\codex-pytest` worked for `--basetemp` and `cache_dir`.

### Suggested Fix
Run pytest for this plugin with explicit writable paths:
```powershell
pytest tests --basetemp C:\Users\65168\AppData\Local\Temp\codex-pytest\self-improve -o cache_dir=C:\Users\65168\AppData\Local\Temp\codex-pytest\cache
```

### Metadata
- Reproducible: yes
- Related Files: pytest.ini

---

## [ERR-20260515-002] missing-ripgrep-and-cache-permissions

**Logged**: 2026-05-15T03:25:00+08:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary
This Windows plugin workspace does not currently have `rg` on PATH, and pytest cache writes under the repo can raise access warnings.

### Error
```text
rg: The term 'rg' is not recognized as a name of a cmdlet, function, script file, or executable program.
PytestCacheWarning: could not create cache path ...\.pytest_cache\...\cache: [WinError 5] Access is denied.
```

### Context
- Command attempted: `rg -n ... -S .`
- Follow-up pytest commands passed when using `--basetemp=C:\Users\65168\AppData\Local\Temp\codex-pytest\e2e`.
- Running the full test suite with `-p no:cacheprovider` avoided cache warnings without changing test behavior.

### Suggested Fix
Use PowerShell `Get-ChildItem | Select-String` when `rg` is unavailable. For this repo, prefer:
```powershell
python -m pytest tests --basetemp=C:\Users\65168\AppData\Local\Temp\codex-pytest\e2e -p no:cacheprovider
```

### Metadata
- Reproducible: yes
- Related Files: pytest.ini
- See Also: ERR-20260515-001

---
