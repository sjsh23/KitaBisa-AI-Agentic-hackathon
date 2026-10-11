"""S/4-shaped data access (CSV now, DynamoDB or OData later). PRD section 13."""
from adapters.csv_adapter import CsvAdapter, default_adapter

__all__ = ["CsvAdapter", "default_adapter"]
