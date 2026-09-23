# Re-establish all integrator, force-field and output context after read_restart.
include protocol.inc
dump trajectory all custom 1000 trajectory.${fs2_segment}.lammpstrj id type x y z vx vy vz
dump_modify trajectory sort id format float %.15g
timer timeout ${fs2_segment_seconds} every 100
run 51000 upto
write_restart state.restart
print "$(step:%.0f)" file progress.txt screen no
