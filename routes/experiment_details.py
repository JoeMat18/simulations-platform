import threading
import streamlit as st
import pandas as pd
from datetime import datetime
from pymongo import MongoClient
from bson import ObjectId
import os
import zipfile
import io

from floodns.external.simulation.main import local_run_single_job
from floodns.external.schemas.routing import Routing
from db_client import experiments_collection



def fetch_experiment_details(simulation_id):
    try:
        experiment = experiments_collection.find_one({"_id": ObjectId(simulation_id)})
        if experiment:
            experiment['_id'] = str(experiment['_id'])  # Convert ObjectId to string
            return experiment
        else:
            st.error("Experiment not found")
            return None
    except Exception as e:
        st.error(f"Error fetching experiment details: {e}")
        return None


def re_run_experiment(simulation_id):
    print(">>> re_run_experiment CALLED!", simulation_id)
    st.write(">>> re_run_experiment CALLED!", simulation_id)

    # Update status first
    experiments_collection.update_one(
        {"_id": ObjectId(simulation_id)},
        {"$set": {"state": "Re-Running"}}
    )

    # Create a background thread for the long-running operation
    def run_in_background():
        try:
            experiment = experiments_collection.find_one({"_id": ObjectId(simulation_id)})
            if not experiment:
                print("Experiment not found for re-run.")
                return

            params = experiment["params"]
            num_jobs, num_cores, ring_size, routing_str, seed = params.split(",")
            model = "BLOOM"
            routing_enum = Routing[routing_str]

            print("Let's launch local_run_single_job...")

            # Ensure all path-related operations use correct paths
            proc = local_run_single_job(
                seed=int(seed),
                n_core_failures=int(num_cores),
                ring_size=int(ring_size),
                model=model,
                alg=routing_enum
            )
            print("local_run_single_job completed.")

            # Update the status when done
            experiments_collection.update_one(
                {"_id": ObjectId(simulation_id)},
                {
                    "$set": {
                        "state": "Finished",
                        "end_time": datetime.now().isoformat(),
                    }
                }
            )
            print("Experiment re-run successfully!")
        except Exception as e:
            print(f"Error in background thread: {e}")
            experiments_collection.update_one(
                {"_id": ObjectId(simulation_id)},
                {"$set": {"state": "Error", "error": str(e)}}
            )

    # Start the background thread
    thread = threading.Thread(target=run_in_background)
    thread.daemon = True
    thread.start()

    # Immediately return to keep the UI responsive
    st.success("Experiment re-run started in the background. Please check back later for results.")


# Function to handle saving edited experiments
def save_edited_experiment(simulation_id):
    try:
        updated_params = f"{st.session_state.params['num_jobs']},{st.session_state.params['num_cores']},{st.session_state.params['ring_size']},{st.session_state.params['routing']},{st.session_state.params['seed']}"
        experiments_collection.update_one(
            {"_id": ObjectId(simulation_id)},
            {
                "$set": {
                    "params": updated_params,
                    "simulation_name": st.session_state.params["simulation_name"],
                }
            },
        )
        st.session_state.show_modal = False
        st.success("Experiment updated successfully!")
    except Exception as e:
        st.error(f"Error updating experiment: {e}")


def delete_experiment(simulation_id):
    try:
        experiments_collection.delete_one({"_id": ObjectId(simulation_id)})
        st.session_state.experiment = None
        st.success("Experiment deleted successfully!")
        st.rerun()
    except Exception as e:
        st.error(f"Error deleting experiment: {e}")


def display_page(simulation_id):
    tab1, tab2 = st.tabs(["Experiment Details", "Chat"])

    with tab1:  # Fetch experiment details if not already loaded
        if "experiment" not in st.session_state or not st.session_state.experiment:
            st.session_state.experiment = fetch_experiment_details(simulation_id)

        if st.session_state.experiment:
            experiment = st.session_state.experiment

            st.header(f"Simulation Name: {experiment['simulation_name']}")
            col1, col2, col3 = st.columns([1, 1, 1])
            with col1:
                st.button("Re-run", on_click=lambda: re_run_experiment(simulation_id))
            with col2:
                st.button("Edit", on_click=lambda: st.session_state.update(show_modal=True))
            with col3:
                st.button("Delete", on_click=lambda: delete_experiment(simulation_id))
            st.subheader("Summary")
            st.write(f"Date: {experiment['date']}")
            st.write(f"Start time: {experiment['start_time']}")
            st.write(f"End time: {experiment['end_time']}")
            st.write(f"State: {experiment['state']}")

            if experiment.get("state") == "Finished" and experiment.get("run_dir"):
                run_dir = experiment["run_dir"]

                # Создаём ZIP в памяти
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for root, dirs, files in os.walk(run_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, run_dir)  # путь внутри архива
                            zipf.write(file_path, arcname)

                zip_buffer.seek(0)  # Ставим указатель в начало файла

                st.subheader("📦 Simulation Output Files")
                st.download_button(
                    label="⬇️ Download All Output Files (.zip)",
                    data=zip_buffer,
                    file_name="simulation_output.zip",
                    mime="application/zip"
                )
            else:
                st.info("⚠️ No output files found or simulation not finished.")

            st.subheader("Parameters")
            params_array = experiment["params"].split(",")
            params_dict = {
                "Num Jobs": params_array[0],
                "Num Cores": params_array[1],
                "Ring Size": params_array[2],
                "Routing Algorithm": params_array[3],
                "Seed": params_array[4],
            }
            st.write(pd.DataFrame([params_dict]))

            # Modal for editing
            if st.session_state.get("show_modal", False):
                with st.form(key="edit_experiment_form"):
                    st.text_input("Simulation Name", key="simulation_name", value=experiment["simulation_name"])
                    st.text_input("Num Jobs", key="num_jobs", value=params_array[0])
                    # Для индекса selectbox сделаем небольшую логику:
                    possible_cores = [1, 4, 8]
                    core_index = possible_cores.index(int(params_array[1])) if int(params_array[1]) in possible_cores else 0

                    st.selectbox("Num Cores", possible_cores, key="num_cores", index=core_index)

                    possible_ring = [2, 4, 8]
                    ring_index = possible_ring.index(int(params_array[2])) if int(params_array[2]) in possible_ring else 0

                    st.selectbox("Ring Size", possible_ring, key="ring_size", index=ring_index)

                    possible_routings = ["ecmp", "ilp_solver", "simulated_annealing"]
                    routing_index = possible_routings.index(params_array[3]) if params_array[3] in possible_routings else 0

                    st.selectbox("Routing Algorithm", possible_routings, key="routing", index=routing_index)
                    st.text_input("Seed", key="seed", value=params_array[4])
                    st.form_submit_button("Save", on_click=lambda: save_edited_experiment(simulation_id))

    with tab2:
        st.title("Chat")

        # Initialize chat messages in session state if not already present
        if "chat_messages" not in st.session_state:
            st.session_state.chat_messages = []

        # Display chat messages
        for message in st.session_state.chat_messages:
            st.markdown(f"**User:** {message}")

        # Input bar for new messages
        new_message = st.text_input("Type your message here...")

        # Submit button for new messages
        if st.button("Send"):
            if new_message:
                st.session_state.chat_messages.append(new_message)
                st.rerun()


def main():
    st.title("Experiment Details")

    # Get simulation_id from URL
    simulation_id = st.query_params["simulation_id"] if "simulation_id" in st.query_params else None

    if simulation_id:
        display_page(simulation_id)
    else:
        st.error("Simulation ID is missing from the URL.")


main()
