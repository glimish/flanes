from setuptools import find_packages, setup

setup(
    name="vex",
    version="0.3.0",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "vex=vex.cli:main",
        ],
    },
    python_requires=">=3.10",
    description="Vex — Version Control for Agentic AI Systems",
    author="Kim",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Version Control",
        "Programming Language :: Python :: 3.12",
    ],
)
