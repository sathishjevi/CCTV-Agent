# Contributing to DeepCamera

Thank you for your interest in contributing to DeepCamera! This project is evolving into an open-source AI skill platform for [SharpAI Aegis](https://sharpai.org).

## How to Contribute

### 🛠️ Build a New Skill

The best way to contribute is by building a new skill. Each skill is a self-contained folder under `skills/` with:

1. **`SKILL.md`** — declares parameters (rendered as UI in Aegis) and capabilities
2. **`requirements.txt`** — Python dependencies
3. **`scripts/`** — entry point using JSON-lines stdin/stdout protocol

See [`skills/detection/yolo-detection-2026/`](skills/detection/yolo-detection-2026/) for a complete reference implementation.

### 📋 Skill Ideas We Need

- Camera providers: Eufy, Reolink, Tapo, Ring
- Messaging channels: Matrix, LINE, Signal
- Automation triggers: MQTT, webhooks
- AI models: VLM scene analysis, SAM2 segmentation, depth estimation

### 🐛 Report Issues

- Use [GitHub Issues](https://github.com/SharpAI/DeepCamera/issues)
- Include your platform, Python version, and steps to reproduce

### 📝 Improve Documentation

- Fix typos, improve clarity, add examples
- Add platform-specific setup guides under `docs/`

## Development Setup

```bash
git clone https://github.com/SharpAI/DeepCamera.git
cd DeepCamera

# Work on a skill
cd skills/detection/yolo-detection-2026
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Code Style

- Python: follow PEP 8
- Use type hints where practical
- Add docstrings to public functions

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
