#include "A.h"

void query () {

    //scan S
    vector<vector<int> > S = vector<vector<int> >();
    ifstream f0("S");
    int count0 = 0;
    vector<int> tmp_vector0 = vector<int>();
    while (!f0.eof()) {
    	int j;
    	f0 >> j;
    	tmp_vector0.push_back(j);
    	count0++;
    	if (count0 == 2) {
    		count0 = 0;
    		S.push_back(tmp_vector0);
    		tmp_vector0 = vector<int>();
    	}
    }
    f0.close();
    
    
    
    
    //scan R
    vector<vector<int> > R = vector<vector<int> >();
    ifstream f1("R");
    int count1 = 0;
    vector<int> tmp_vector1 = vector<int>();
    while (!f1.eof()) {
    	int j;
    	f1 >> j;
    	tmp_vector1.push_back(j);
    	count1++;
    	if (count1 == 2) {
    		count1 = 0;
    		R.push_back(tmp_vector1);
    		tmp_vector1 = vector<int>();
    	}
    }
    f1.close();
    
    
    
    
    //scan U
    vector<vector<int> > U = vector<vector<int> >();
    ifstream f2("U");
    int count2 = 0;
    vector<int> tmp_vector2 = vector<int>();
    while (!f2.eof()) {
    	int j;
    	f2 >> j;
    	tmp_vector2.push_back(j);
    	count2++;
    	if (count2 == 2) {
    		count2 = 0;
    		U.push_back(tmp_vector2);
    		tmp_vector2 = vector<int>();
    	}
    }
    f2.close();
    
    
    
    
    //scan T
    vector<vector<int> > T = vector<vector<int> >();
    ifstream f3("T");
    int count3 = 0;
    vector<int> tmp_vector3 = vector<int>();
    while (!f3.eof()) {
    	int j;
    	f3 >> j;
    	tmp_vector3.push_back(j);
    	count3++;
    	if (count3 == 2) {
    		count3 = 0;
    		T.push_back(tmp_vector3);
    		tmp_vector3 = vector<int>();
    	}
    }
    f3.close();
    
    
    
    
    //hash R
    map<int, vector<vector<int> > > R1_hash;
    for (int i = 0; i < R.size(); i++) {
    	if (R1_hash.find(R[i][1]) == R1_hash.end()) {
    		R1_hash[R[i][1]] = vector<vector<int> > ();
    	}
    	R1_hash[R[i][1]].push_back(R[i]);
    }
    
    
    
    
    //hash U
    map<int, vector<vector<int> > > U1_hash;
    for (int i = 0; i < U.size(); i++) {
    	if (U1_hash.find(U[i][1]) == U1_hash.end()) {
    		U1_hash[U[i][1]] = vector<vector<int> > ();
    	}
    	U1_hash[U[i][1]].push_back(U[i]);
    }
    
    
    
    
    //hash T
    map<int, vector<vector<int> > > T1_hash;
    for (int i = 0; i < T.size(); i++) {
    	if (T1_hash.find(T[i][1]) == T1_hash.end()) {
    		T1_hash[T[i][1]] = vector<vector<int> > ();
    	}
    	T1_hash[T[i][1]].push_back(T[i]);
    }
    
    
    
    
    //loop over S
    for (int index0 = 0; i < S.size(); ++index0) {
        if (!(S[1]==50)) {
            continue;
        }
        if (R1_hash.find(S[index0][0]) == R1_hash.end()) {
            continue;
        }
        vector<vector<int> > table1 = R1_hash[S[index0][0]];
    
    
    
        //loop over table1
        for (int index1 = 0; index1 < table1.size(); ++index1) {
            //if there is no match, continue
            if (U1_hash.find(table1[index1][0]) == U1_hash.end()) {
                continue;
            }
            vector<vector<int> > table2 = U1_hash[table1[index1][0]];
        
        
        
            //loop over table2
            for (int index2 = 0; i < table2.size(); ++index2) {
                if (!(100==table2[0])) {
                    continue;
                }
                if (T1_hash.find(table2[index2][0]) == T1_hash.end()) {
                    continue;
                }
                vector<vector<int> > table3 = T1_hash[table2[index2][0]];
            
            
            
                //loop over final join results
                for (int index3 = 0; index3 < table3.size(); ++index3) {
                    if ((100==table3[1] && table3[0]==50) && S[1]==table3[0]) {
                        //emit result
                    }
                }
            }
        }
    }

}

int main() { query(); }