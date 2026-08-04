#!/usr/bin/env python3
"""
Example: Extract structured data from EDGAR text using local model.
Shows hardware probe → config → inference pattern.
"""

import json
from config import ScubberConfig
from ollama_client import OllamaClient


def extract_company_info(text: str, config: ScubberConfig) -> dict:
    """
    Extract company name, CIK, filing type from EDGAR document text.

    Args:
        text: raw EDGAR filing text (excerpt)
        config: ScubberConfig (hardware profile + Ollama connection)

    Returns:
        dict with extracted fields (name, cik, filing_type, confidence)
    """
    client = OllamaClient(config.ollama_config)

    prompt = f"""Extract the following from the SEC EDGAR filing below:
- Company legal name
- CIK (Central Index Key, a number)
- Filing type (e.g., 10-K, 10-Q, 8-K)

Return as JSON only, e.g.: {{"company_name": "...", "cik": "...", "filing_type": "..."}}
If a field is not found, use null.

FILING TEXT:
{text[:2000]}  # Limit to first 2000 chars for demo
"""

    response = client.chat_completion(
        messages=[
            {
                "role": "system",
                "content": "You are an SEC EDGAR document analyst. Extract data in valid JSON.",
            },
            {"role": "user", "content": prompt},
        ]
    )

    content = response["choices"][0]["message"]["content"]

    # Parse JSON from response
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Try to extract JSON block
        import re

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"error": "Could not parse JSON", "raw": content}


def main():
    """Initialize config and run extraction example."""
    print("EDGAR Scrubber — Example Usage")
    print("=" * 60)

    # 1. Load or probe configuration
    config = ScubberConfig()
    print(config.report())

    # 2. Example EDGAR text (simplified)
    example_text = """
    UNITED STATES SECURITIES AND EXCHANGE COMMISSION
    Washington, D.C. 20549

    FORM 10-K
    ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)
    OF THE SECURITIES EXCHANGE ACT OF 1934

    For the fiscal year ended December 31, 2023

    Commission File Number 0-00000

    APPLE INC.
    (Exact name of registrant as specified in its charter)

    CIK: 0000320193

    Business Description: Apple Inc. designs, manufactures, and markets smartphones, computers,
    tablets, wearables, and related services worldwide.
    """

    print("Running extraction on sample EDGAR text...")
    print()

    try:
        result = extract_company_info(example_text, config)
        print("Extracted data:")
        print(json.dumps(result, indent=2))
    except RuntimeError as e:
        print(f"Error: {e}")
        print("\nMake sure Ollama is running:")
        print("  1. Start Ollama: ollama serve")
        print("  2. Pull model: ollama pull qwen2.5:7b-instruct-q4_K_M")
        print(f"  3. Verify: curl http://localhost:11434/api/tags")


if __name__ == "__main__":
    main()
