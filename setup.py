from setuptools import find_packages, setup

setup(
    name="flanes",
    version="0.4.2",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "flanes=flanes.cli:main",
        ],
    },
    python_requires=">=3.10",
    description="Flanes: Feature Lanes for Agents - version control for agentic AI systems",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Version Control",
        "Programming Language :: Python :: 3.12",
    ],
)
