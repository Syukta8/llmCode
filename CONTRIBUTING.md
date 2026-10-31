# Contributing to llmCode

Thank you for your interest in contributing to `llmCode`!

---

## Code of Conduct

Be respectful, constructive, and collaborative. Harassment or abusive behavior of any kind will not be tolerated.

---

## Development Workflow

1. **Fork & Clone**:
   ```bash
   git clone https://github.com/<your-username>/llmCode.git
   cd llmCode
   ```

2. **Branching**:
   - Create a feature or bugfix branch off `main`:
     ```bash
     git checkout -b feat/my-improvement
     # or
     git checkout -b fix/issue-description
     ```

3. **Prerequisites & Setup**:
   - Run setup for your OS:
     - Linux / macOS: `./setup.sh`
     - Windows: `setup.bat`

4. **Running Tests**:
   - All tests must pass before submitting a pull request:
     ```bash
     src/LLMServer/.venv/bin/python3 -m unittest discover -s src/LLMServer/tests
     # on Windows:
     src\LLMServer\.venv\Scripts\python.exe -m unittest discover -s src\LLMServer\tests
     ```

5. **Submitting a Pull Request**:
   - Ensure code conforms to clean Python standards and existing conventions.
   - Do not commit generated binary artifacts, models (`*.gguf`), logs, or credentials.
   - Provide clear PR descriptions and link any relevant issues.
