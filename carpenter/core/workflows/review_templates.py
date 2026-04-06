"""Review template registry for output type → review function mapping.

Maps output types to their automated review functions. Adding new types
is a kernel change requiring a human-approved release.
"""

# Output types with built-in automated review capability.
# Python code goes through the existing review pipeline (AST check + AI review).
_AUTOMATED_REVIEW_TYPES = {"python"}


def has_automated_review(output_type: str) -> bool:
    """Check if an output type has automated review support.

    Args:
        output_type: The output type string (e.g. 'python', 'text').

    Returns:
        True if automated review is available, False if human review needed.
    """
    return output_type in _AUTOMATED_REVIEW_TYPES


def get_review_template(output_type: str):
    """Get the review function for an output type.

    Args:
        output_type: The output type string.

    Returns:
        Review function, or None if no automated review available.
    """
    if output_type == "python":
        try:
            from ...review.pipeline import run_review_pipeline
            return run_review_pipeline
        except ImportError:
            return None
    return None
