from setuptools import setup, find_packages

setup(
    name="juste-des-ventilateurs",
    version="0.1.0",
    description="Predictive maintenance and fan control service for jumeaux-chauds",
    packages=find_packages(exclude=["tests*", "notebooks*"]),
    python_requires=">=3.11",
)
