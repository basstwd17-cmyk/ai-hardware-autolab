prj_project open "Delay_test.ldf"
prj_run Synthesis -impl impl1
prj_run Translate -impl impl1
prj_run Map -impl impl1
prj_run PAR -impl impl1
prj_run Export -impl impl1
prj_project close
