from setuptools import setup, find_packages

setup(
    name="ibkr-connector",
    version="1.0.0",
    author="Zehnlabs Rebalancer Team",
    description="Interactive Brokers connector implementation",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={
        "ibkr_connector": ["py.typed"],
    },
    install_requires=[
        "broker-connector-base==1.0.0",
        "rebalance-calculator==1.0.0",
        "app-config==1.0.0",
        "ib-async==2.0.1",
        "pydantic==2.11.7",
        "aiohttp==3.12.15",
        "PyYAML==6.0.2",
    ],
    python_requires=">=3.11",
)
