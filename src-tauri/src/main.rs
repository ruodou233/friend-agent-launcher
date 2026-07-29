fn main() {
    if friend_agent_launcher_lib::run_credential_helper_if_requested() {
        return;
    }
    friend_agent_launcher_lib::run();
}
