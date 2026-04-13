"""
Claude Service
Generates briefing scripts using the Anthropic API.
"""

import anthropic

LENGTH_WORDS = {"quick": 200, "standard": 420, "deep": 820}

TONE_DESCRIPTIONS = {
    "analytical": "data-driven, precise, investment-focused — highlight numbers, trends, and implications",
    "executive": "high-level, strategic, concise — lead with the 'so what', skip the backstory",
    "conversational": "warm, intelligent, like a well-read friend — insightful but never stiff",
}


class ClaudeService:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    async def generate_briefing(
        self,
        articles: list[dict],
        user_profile: dict,
        briefing_profile: dict,
    ) -> str:
        """
        Generate a podcast-style briefing script tailored to the user
        and their specific briefing profile.
        """
        name = user_profile.get("name", "")
        user_context = user_profile.get("context", "")
        user_interests = user_profile.get("interests", "")

        profile_context = briefing_profile.get("context", "")
        tone = briefing_profile.get("tone", "analytical")
        length = briefing_profile.get("length", "standard")
        scope = briefing_profile.get("scope", "email")

        word_target = LENGTH_WORDS.get(length, 420)
        tone_desc = TONE_DESCRIPTIONS.get(tone, TONE_DESCRIPTIONS["analytical"])

        # Build article context
        article_text = "\n\n---\n\n".join([
            f"SOURCE: {a.get('publication', 'Unknown')} ({a.get('category', 'News')})\n"
            f"HEADLINE: {a.get('headline', '')}\n"
            f"CONTENT: {a.get('body', a.get('excerpt', ''))[:800]}"
            for a in articles
        ])

        system_prompt = f"""You are writing a personal morning briefing podcast script.

ABOUT THE LISTENER:
{f'Name: {name}' if name else ''}
{f'Context: {user_context}' if user_context else ''}
{f'Interests: {user_interests}' if user_interests else ''}

THIS BRIEFING'S SPECIFIC INSTRUCTIONS:
{profile_context if profile_context else 'Cover all stories with insight and relevance.'}

STYLE REQUIREMENTS:
- Tone: {tone_desc}
- Target length: approximately {word_target} words
- Format: Pure flowing prose designed to be listened to — no bullet points, no headers, no markdown
- Open with a strong, engaging hook
- Cover each story with context and insight, not just facts — tell them what it means
- Where relevant, connect stories to the listener's world (their role, region, interests)
- Close with a brief forward-looking thought
- Write as if speaking directly to {name if name else 'the listener'} — make it feel personal

This is audio. Every sentence should sound natural when spoken aloud."""

        user_message = article_text
        if scope == "web":
            user_message = (
                f"Here are today's newsletter stories. Write the briefing covering these, "
                f"and also weave in broader global context where relevant "
                f"(markets, AI, geopolitics, NZ business):\n\n{article_text}"
            )
        else:
            user_message = f"Here are today's newsletter stories. Write the briefing:\n\n{article_text}"

        message = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        return message.content[0].text

    async def generate_summary(self, articles: list[dict]) -> str:
        """Generate a short one-paragraph daily summary."""
        headlines = "\n".join([
            f"- {a.get('publication', '')}: {a.get('headline', '')}"
            for a in articles[:10]
        ])

        message = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": f"Write a 2-3 sentence summary of today's top stories for a busy executive. Be specific. No fluff.\n\nStories:\n{headlines}"
            }],
        )
        return message.content[0].text
