from setuptools import setup, find_packages

setup(
    name="broker-connector-base",
    version="1.0.0",
    author="Zehnlabs Rebalancer Team",
    description="Abstract base classes for broker connectors",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={
        "broker_connector_base": ["py.typed"],
    },
    install_requires=[
        "pydantic==2.11.7",
    ],
    python_requires=">=3.11",
)
