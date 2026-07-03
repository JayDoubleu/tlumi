"""Random resources: no cloud credentials needed.

Demonstrates tlumi basics using the pulumi-random provider:
- Variables from tlumi.yaml
- Secret values (RandomPassword)
- Python loops (creating multiple resources)
- Pulumi exports
"""

import pulumi
from pulumi_random import RandomId, RandomPassword, RandomPet

config = pulumi.Config()
pet_count = int(config.get("pet_count") or "3")

# A random ID (useful for unique naming)
unique_id = RandomId("unique-id", byte_length=8)

# A random password (stored as a secret in state)
db_password = RandomPassword(
    "db-password",
    length=32,
    special=True,
)

# Multiple random pets using a Python loop
pets = []
for i in range(pet_count):
    pet = RandomPet(f"pet-{i}", length=2)
    pets.append(pet)

pulumi.export("unique_id", unique_id.hex)
pulumi.export("db_password", pulumi.Output.secret(db_password.result))
pulumi.export("pet_names", [pet.id for pet in pets])
