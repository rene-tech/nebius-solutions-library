# Reduced Lennard-Jones units; do not label elapsed model time as nanoseconds.
pair_style lj/cut 2.5
pair_coeff 1 1 1.0 1.0 2.5
neighbor 0.3 bin
neigh_modify delay 0 every 1 check yes
fix integrator all nve
timestep 0.005
thermo 500
thermo_style custom step atoms temp pe ke etotal press vol
thermo_modify lost error flush yes format float %.15g
