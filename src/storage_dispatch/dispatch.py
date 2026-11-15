from fractions import Fraction

def simulate(capacity,initial,max_charge,max_discharge,eta_c,eta_d,requests):
    soc=Fraction(initial);total_in=total_out=losses=unmet=Fraction();steps=[]
    for i,r in enumerate(requests):
        before=soc;grid_in=grid_out=loss=Fraction()
        if r>=0:
            grid_in=min(Fraction(r),Fraction(max_charge),(capacity-soc)/eta_c);soc+=grid_in*eta_c;loss=grid_in*(1-eta_c);missing=r-grid_in
        else:
            grid_out=min(Fraction(-r),Fraction(max_discharge),soc*eta_d);soc-=grid_out/eta_d;loss=grid_out/eta_d-grid_out;missing=-r-grid_out
        total_in+=grid_in;total_out+=grid_out;losses+=loss;unmet+=missing
        steps.append({'slot':i,'request':r,'before':str(before),'after':str(soc),'grid_in':str(grid_in),'grid_out':str(grid_out),'loss':str(loss),'unmet':str(missing)})
    return {'steps':steps,'final':str(soc),'grid_in':str(total_in),'grid_out':str(total_out),'loss':str(losses),'unmet':str(unmet),'conservation_residual':str(initial+total_in-total_out-losses-soc)}
