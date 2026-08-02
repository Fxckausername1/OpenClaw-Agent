// Black-Scholes European option pricer (calls & puts)
function normPdf(x){ return Math.exp(-0.5*x*x)/Math.sqrt(2*Math.PI); }
// CDF using Abramowitz and Stegun approximation
function normCdf(x){
  var k = 1.0/(1.0+0.2316419*Math.abs(x));
  var a1=0.319381530, a2=-0.356563782, a3=1.781477937, a4=-1.821255978, a5=1.330274429;
  var poly = (((a5*k + a4)*k + a3)*k + a2)*k + a1;
  var approx = 1.0 - normPdf(x)*poly*k;
  return x >= 0 ? approx : 1.0 - approx;
}

function bsPrice(S,K,r,sigma,t,optionType){
  // S: spot, K: strike, r: risk-free rate (annual), sigma: vol (annual), t: time in years
  if(t<=0) return Math.max(optionType==='call'?S-K:K-S,0);
  if(sigma<=0){ return Math.max(optionType==='call'?S-K*Math.exp(-r*t):K*Math.exp(-r*t)-S,0); }
  var d1 = (Math.log(S/K) + (r + 0.5*sigma*sigma)*t) / (sigma*Math.sqrt(t));
  var d2 = d1 - sigma*Math.sqrt(t);
  if(optionType==='call'){
    return S*normCdf(d1) - K*Math.exp(-r*t)*normCdf(d2);
  }else{
    return K*Math.exp(-r*t)*normCdf(-d2) - S*normCdf(-d1);
  }
}

module.exports = { bsPrice };

