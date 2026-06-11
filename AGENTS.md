# AGENTS.md

Guidance for AI coding assistants (GitHub Copilot, Anthropic Claude Code, etc.) working in this repository.

For project architecture, definitions, scope, and associated commands, see [README.md](README.md).

---

## Functional Coding Standards

- Follow prominent established engineering best practices: the Single Responsibility Principle, the Liskov Substitution Principle, Don't Repeat Yourself, Keep it Simple, Stupid, and the Principle of Least Knowledge.
- Pay heed to static analysis linting warnings, especially those related to potential undefined values, type mismatches, and unreachable code, and additionally, complexity. When following the Single Responsibility Principle, complexity is often reduced naturally.
- In general, avoid using ternary statements. Using them for simple variable declaration is fine, but as soon as significant logic would be added, we prefer if, then, else assignments, with comments.
- Optimize code for clarity and re-use, avoiding overly cute single-line solutions, niche understanding of a language's implementation, and unnecessary cognitive overhead.
- Pay attention to significant performance implications of code, especially in loops or frequently called functions or common flows, but do not micro-optimize.

---

## Development Workflow (to minimize regressions):

- Check `requirements.txt` and test suite before making changes
- **Read existing code thoroughly** - Before modifying any code, read through ALL functions and code paths that will be affected by the change. Trace the full execution flow to understand side effects. Static analysis linting warnings and errors are often very insightful as to what may have been missed, so check them first, especially applying auto-fixes.
- **Test-Driven Development preference** - When starting new projects or major features or beginning rewrites, suggest writing/updating tests first. For existing code without tests, offer to create test coverage before making risky changes. Tests really do prevent many kinds of avoidable regressions.
- **Ask clarifying questions up front** - When requirements are ambiguous or incomplete, ask specific questions before implementing. Don't guess and hope we will correct you after seeing the broken result.
- **Make incremental changes** - Limit each change to ONE logical modification. If adding a feature requires touching multiple functions, explain the plan first and consider breaking it into smaller steps. Don't bundle multiple features into one implementation.
- **Maintain a mental requirements checklist** - When adding new functionality, explicitly verify that existing requirements still hold. For complex systems, list out the key constraints before making changes.
- **Review code before submitting** - Check for simple errors (undefined variables, wrong parameter names, type mismatches) before presenting code. These should be caught before submission, not after execution.
- **Acknowledge complexity honestly** - If a change touches a complex multi-pass system or has many edge cases, say so and recommend extra caution or testing rather than assuming it'll work.

---

## Code Style

- Use modern Python features (Python 3.11+)
- Follow PEP 8 style guidelines
- Use f-strings for string formatting
- Prefer functional programming patterns where appropriate
- Maintain consistent naming: PascalCase for classes, snake_case for functions/variables
- Keep curly braces for single-line blocks (not Python-specific, but applies to any embedded shell scripts)
- Add docstrings for all functions with descriptions justifying existence

---

### Naming Conventions

- Attempt to be reasonably self-documenting with variable names, with a max character length of about 20 characters. Avoid single-character variables, with the sole exception of throwaway iterable indices.
- Use PascalCase for class names.
- Use snake_case for functions, variables, and methods.
- Use ALL_CAPS (screaming snake case) for constants.

---

## Testing

- All tests use pytest and run without audio hardware (all capture is mocked)
- Run tests with `pytest tests/ -v` from repo root
- Test coverage includes: audio capture, DSP pipeline, filters, state management, storage, API endpoints
- Before making DSP/filter changes, run existing tests to ensure no regressions
- Add tests for new features following established patterns in `tests/`

---

## Terminal Command Safety (VS Code crash prevention)

- **VS Code can crash from terminal output flooding.** Certain patterns overwhelm the integrated terminal and cause editor instability or crashes
- **Always limit output when testing unfamiliar scripts** - Redirect to a file (`> .TMP_output.log 2>&1`) when running scripts that might produce substantial output
- **Avoid rapid progress indicators** - Progress bars using carriage returns (`\r`) or ANSI escape codes can create extremely long logical "lines" that destabilize the terminal. Prefer simple periodic line-based progress (e.g., "Processed 100/500 files...")

---

## Debugging

- When debugging troublesome code, provide summaries of the paths being proven or disproven
- Provide more verbose (but concise) logging by default, so that if something goes wrong it is much more obvious, instead of needing to add and then remove logging throughout the entire process

---

## Documentation

- When making functional code changes, attempt to keep any README documentation, especially about usage or execution order, up to date.
- Add docstrings for functions with descriptions justifying existence
- Include inline comments for confusing or error-prone code segments

---

## Python Scripts

When generating Python scripts, follow the module pattern:

```python
#!/usr/bin/env python3
"""
Script description here.
"""

import sys
# other imports...

def main():
    """Main entry point."""
    pass

if __name__ == "__main__":
    main()
```

---

## Repository-Specific Notes

### Deployment Architecture

This project uses a **symlinked versioning layout** for Raspberry Pi deployment:
- Code lives in versioned directories: `/opt/noise-warden/noise-warden-vXX/`
- Symlink `/opt/noise-warden/current` points to active version
- Data persists in `/opt/noise-warden/shared/` (survives upgrades)
- Deploy script (`deploy_noise_warden.sh`) handles version swapping
- Systemd service runs as `noisewarden` user

### Local Development (Mac/Laptop)

- Project runs without Pi hardware (GPIO/relay features gracefully degrade)
- Use local config override: `config/noise_warden_local.yaml`
- Create local data directories: `local_data/{snippets,playlist,build}`
- Run with: `uvicorn noise_warden.main:app --host 127.0.0.1 --port 8787 --reload --reload-include '*.yaml'`
- Built-in mic used by default; test by clapping near laptop

### Key Architecture Components

**Audio Pipeline**:
- `audio.py`: Continuous callback streaming via sounddevice, thread-safe queue, pre-roll buffer
- `dsp.py`: RMS/dBFS, A-weighting, spectrum features, music scoring
- `engine.py`: State machine, incident lifecycle, exclusion filters, day/night enforcement

**Exclusion Filters** (prevent false positives):
- Impulse (door slams, single bangs)
- Thunder-like (rumble patterns)
- Rain-like (white noise)
- Mower-like (steady mechanical drone)
- Drive-by (amplitude envelope patterns)

**Storage**:
- SQLite database (`shared/noise_warden.db`) with WAL mode
- Incident records with soft delete, pagination, CSV export
- WAV snippet files in `shared/snippets/`
- Automatic cleanup of old autodismissed incidents

**Web Interface**:
- FastAPI backend with REST API
- Static HTML/JS/CSS (no frontend framework)
- Service Worker for offline caching (requires HTTPS/self-signed cert)
- Canvas-based intensity waveform visualization
- Dark mode via localStorage preference

### Critical Dependencies

- **portaudio19-dev**: Required for audio capture (install: `apt install portaudio19-dev`)
- **libsndfile1**: WAV file I/O (install: `apt install libsndfile1`)
- **ffmpeg**: Audio format conversion (install: `apt install ffmpeg` or `brew install ffmpeg`)
- **exiftool**: Optional, for metadata if present (install: `brew install exiftool`)

### Configuration

- Config file: `config/noise_warden.yaml`
- Editable via web UI at `/config` or direct file editing
- Key sections: `audio`, `gpio`, `response`, `rules`, `filters`, `web`
- **Calibration is critical**: Use 3-step wizard at `/calibration`
- Sample rates: 22050 Hz (default), 44100 Hz (CD), 48000 Hz (studio)

### Testing Approach

- Run full suite: `pytest tests/ -v`
- Single test: `pytest tests/test_engine.py::TestDriveByDetection -v`
- All audio operations are mocked (no hardware needed)
- DSP filter tests use synthetic audio and real captured clips

### Known Quirks

- **Dual microphone support**: Reference subtraction plugins exist but not yet wired into engine
- **Self-noise suppression**: Currently time-based cooldown; adaptive filtering planned
- **Pixel Motion Photos**: Referenced in context but not relevant to this project (copy-paste artifact)
- **Ordinance thresholds**: Currently hardcoded in `ordinance.py`, should be externalized to YAML

### Common Tasks

- **Testing DSP changes**: Use `python -m noise_warden.reclassify --all` to replay pipeline against saved snippets
- **Calibration transfer**: Laptop calibration offsets transfer to Pi if using same USB mic+interface
- **Version upgrades**: Run `install_pi.sh` from new version, copy config forward, restart service
- **Rollback**: `cd /opt/noise-warden && ./deploy_noise_warden.sh noise-warden-vXX`

### Legal / Practical Reality

- **Not ANSI Type 1/2 certified**: This is evidence collection, not a certified SPL meter
- **Response mode**: Disabled by default. Use conservatively and test thoroughly.
- **Database backup**: Set up periodic backups of `shared/noise_warden.db` (SD card failure = total loss)

---
