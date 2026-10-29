from ontology_lookup.manager import OntologyDatabaseManager

manager = OntologyDatabaseManager()
manager.prepare_input_folders()
manager.create_database()
