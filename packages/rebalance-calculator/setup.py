from setuptools import setup, find_packages

setup(
    name="rebalance-calculator",
    version="1.0.0",
    author="IBKR Portfolio Rebalancer Team",
    description="Broker-agnostic rebalancing calculation engine",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={
        "rebalance_calculator": ["py.typed"],
    },
    install_requires=[
        "pydantic==2.11.7",
        "broker-connector-base==1.0.0",
    ],
    python_requires=">=3.11",
)
