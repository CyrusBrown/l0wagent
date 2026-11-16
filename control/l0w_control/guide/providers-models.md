# Providers, models and roles

**Providers** (Settings → Providers): the **Claude subscription** (the assistant's own login on
its computer; set up in the setup wizard or with `./l0w claude-login`), an **Anthropic API key**,
and **opencode Go** (a subscription to open models, run with opencode instead of Claude Code;
paste the key from opencode.ai → your workspace → API keys). Keys are write-only and stored
encrypted; only sessions on that provider get them.

**Models**: Claude providers use Claude Code's names, haiku, sonnet and opus, with an effort
(low, medium, high). opencode providers have a list of enabled models: Settings → Providers →
**+ Add models** picks from the provider's catalog (with prices); the order matters (the first is
the default). A model is written with its effort, e.g. `opencode-go/glm-5.3/high`; models without
efforts just use their default.

**Roles** (Settings → Roles): which provider and model each job uses: the **worker** (wakeups and
routines; its allowed models come from the level), the **reviewer**, the **replier** (Reply now),
**chat** (your 1:1 chats' default) and **utility**. The level's "models" limit restricts what the
worker may plan with.

Choosing: cheaper and faster models for replies and routine work; a stronger model for the
worker's hard tasks and the reviewer. Free models on opencode Go cost nothing but may be slower
or less capable.
