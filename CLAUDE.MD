# Important instructions

1. Never commit or push any changes to github. You can only use git commands to read or inspect or understand the codebase, repo or commit history.
2. Always take things like API keys from env using dotenv and NEVER hardcode keys in a code file.
3. For LLM always use ChatGroq from langchain_groq and model as "openai/gpt-oss-120b".
4. For embeddings, use huggingface transformers 'sentence-transformers/all-MiniLM-L6-v2'. Use locally.
5. If running or executing any files or installing any libraries, ensure you use the venv in the root directory always and no other environment or globally.