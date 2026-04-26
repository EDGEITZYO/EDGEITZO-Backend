from neo4j import GraphDatabase
from app.core.settings import settings


def get_neo4j_driver():
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password)
    )
    return driver