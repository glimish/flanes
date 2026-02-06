from setuptools import find_packages, setup

setup(
    name="fla",
    version="0.3.0",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "fla=fla.cli:main",
        ],
    },
    python_requires=">=3.10",
    description="FLA — Feature Lanes for Agents: version control for agentic AI systems",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Version Control",
        "Programming Language :: Python :: 3.12",
    ],
)
