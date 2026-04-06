"""Data model contract validation for arc I/O.

Provides dynamic import and validation of attrs models declared as
contracts on template steps.  Contract references use the format
``module_path:ClassName`` (e.g. ``data_models.dark_factory:ScenarioOutput``).
"""

import importlib
import json
import logging

import cattrs

logger = logging.getLogger(__name__)


def parse_contract_ref(contract_ref: str) -> tuple[str, str]:
    """Parse a contract reference string into (module_path, class_name).

    Args:
        contract_ref: A string like ``data_models.example:TaskResult``.

    Returns:
        A (module_path, class_name) tuple.

    Raises:
        ValueError: If the format is invalid (missing ``:``, empty parts).
    """
    if ":" not in contract_ref:
        raise ValueError(
            f"Invalid contract reference '{contract_ref}': "
            "expected format 'module_path:ClassName'"
        )
    module_path, _, class_name = contract_ref.partition(":")
    if not module_path or not class_name:
        raise ValueError(
            f"Invalid contract reference '{contract_ref}': "
            "both module_path and ClassName must be non-empty"
        )
    return module_path, class_name


def load_model_class(contract_ref: str):
    """Dynamically import and return the attrs model class.

    Args:
        contract_ref: A string like ``data_models.example:TaskResult``.

    Returns:
        The model class.

    Raises:
        ValueError: If the reference format is invalid.
        ImportError: If the module cannot be imported.
        AttributeError: If the class does not exist in the module.
    """
    module_path, class_name = parse_contract_ref(contract_ref)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls


def validate_contract(data, contract_ref: str):
    """Validate data against a contract model.

    Args:
        data: Either a JSON string or a dict/object to validate.
        contract_ref: A string like ``data_models.example:TaskResult``.

    Returns:
        A validated attrs model instance.

    Raises:
        ValueError: If the contract reference is invalid.
        ImportError: If the model module cannot be imported.
        AttributeError: If the model class does not exist.
        cattrs.errors.ClassValidationError: If the data does not conform to the model.
    """
    model_class = load_model_class(contract_ref)

    if isinstance(data, str):
        data = json.loads(data)

    return cattrs.structure(data, model_class)
