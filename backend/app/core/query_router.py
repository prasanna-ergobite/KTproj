"""
Query Intent Router for AutoKT Search Pipeline.
Classifies search queries into structural intents (same module, same file, file symbols, containing file, containing module)
versus open-ended semantic queries, and extracts target entity names (function name, class name, file path).
"""

import re
from enum import Enum
from typing import Optional
from pydantic import BaseModel


class QueryIntent(str, Enum):
    SAME_MODULE_FUNCTIONS = "SAME_MODULE_FUNCTIONS"
    SAME_FILE_FUNCTIONS = "SAME_FILE_FUNCTIONS"
    FILE_SYMBOLS = "FILE_SYMBOLS"
    CONTAINING_FILE = "CONTAINING_FILE"
    CONTAINING_MODULE = "CONTAINING_MODULE"
    SEMANTIC = "SEMANTIC"


class IntentClassificationResult(BaseModel):
    intent: QueryIntent
    entity_name: Optional[str] = None
    raw_query: str


# Regex patterns ordered by specificity
PATTERNS = [
    # SAME_MODULE_FUNCTIONS
    (QueryIntent.SAME_MODULE_FUNCTIONS, re.compile(r"(?:what\s+|list\s+|show\s+|are\s+there\s+)?(?:other\s+)?functions?\s+(?:are\s+in|exist\s+in|are\s+there\s+in|in)?\s*(?:the\s+)?same\s+module\s+as\s+([\w\-]+)", re.IGNORECASE)),
    (QueryIntent.SAME_MODULE_FUNCTIONS, re.compile(r"siblings?\s+of\s+(?:function\s+)?([\w\-]+)", re.IGNORECASE)),
    (QueryIntent.SAME_MODULE_FUNCTIONS, re.compile(r"what\s+else\s+is\s+in\s+the\s+same\s+module\s+as\s+([\w\-]+)", re.IGNORECASE)),

    # SAME_FILE_FUNCTIONS
    (QueryIntent.SAME_FILE_FUNCTIONS, re.compile(r"(?:what\s+|list\s+|show\s+|are\s+there\s+)?(?:other\s+)?functions?\s+(?:are\s+in|exist\s+in|are\s+there\s+in|in)?\s*(?:the\s+)?same\s+file\s+as\s+([\w\-]+)", re.IGNORECASE)),
    (QueryIntent.SAME_FILE_FUNCTIONS, re.compile(r"functions?\s+defined\s+alongside\s+([\w\-]+)", re.IGNORECASE)),

    # FILE_SYMBOLS
    (QueryIntent.FILE_SYMBOLS, re.compile(r"(?:what\s+)?(?:functions?|classes?|symbols?)\s+(?:are\s+)?in\s+([\w\.\/\-]+\.py)", re.IGNORECASE)),
    (QueryIntent.FILE_SYMBOLS, re.compile(r"(?:show|list)\s+(?:me\s+)?(?:the\s+)?(?:functions?|classes?|contents?\s+of\s+)?([\w\.\/\-]+\.py)", re.IGNORECASE)),
    (QueryIntent.FILE_SYMBOLS, re.compile(r"what(?:'s|\s+is)\s+in\s+([\w\.\/\-]+\.py)", re.IGNORECASE)),
    (QueryIntent.FILE_SYMBOLS, re.compile(r"symbols?\s+in\s+([\w\.\/\-]+\.py)", re.IGNORECASE)),

    # CONTAINING_FILE
    (QueryIntent.CONTAINING_FILE, re.compile(r"(?:which|what)\s+file\s+(?:contains?|has|defines?)\s+(?:function\s+|class\s+)?([\w\-]+)", re.IGNORECASE)),
    (QueryIntent.CONTAINING_FILE, re.compile(r"where\s+is\s+(?:function\s+|class\s+)?([\w\-]+)\s+defined", re.IGNORECASE)),
    (QueryIntent.CONTAINING_FILE, re.compile(r"file\s+location\s+of\s+([\w\-]+)", re.IGNORECASE)),

    # CONTAINING_MODULE
    (QueryIntent.CONTAINING_MODULE, re.compile(r"(?:which|what)\s+module\s+(?:contains?|has|is)\s+([\w\.\/\-]+(?:\.py)?)\s*(?:in)?", re.IGNORECASE)),
]


def classify_query_intent(query: str) -> IntentClassificationResult:
    """
    Classify natural language search query into structural vs semantic intent
    and extract symbol/filename entities if present.
    """
    if not query or not query.strip():
        return IntentClassificationResult(intent=QueryIntent.SEMANTIC, entity_name=None, raw_query=query)

    clean_query = query.strip()

    for intent, pattern in PATTERNS:
        match = pattern.search(clean_query)
        if match:
            extracted_entity = match.group(1).strip()
            # Clean trailing function call parens if user typed "summarize()"
            if extracted_entity.endswith("()"):
                extracted_entity = extracted_entity[:-2]
            return IntentClassificationResult(
                intent=intent,
                entity_name=extracted_entity,
                raw_query=query,
            )

    return IntentClassificationResult(intent=QueryIntent.SEMANTIC, entity_name=None, raw_query=query)
